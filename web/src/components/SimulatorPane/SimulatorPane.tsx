import { useCallback, useEffect, useRef, useState } from "react";
import { Camera, Loader2, Pause, Play, RotateCw, Smartphone, Video, X } from "lucide-react";

import { hostFetch } from "@/lib/host";
import { isElectronShell } from "@/lib/nativeBridge";
import { useChatStore } from "@/store/chatStore";
import { cn } from "@/lib/utils";

/** How often the frame fallback pulls a picture (simctl screenshot ~3-5 fps). */
const FRAME_INTERVAL_MS = 800;

/** The codec the stream route encodes with — checked before opening a buffer. */
const STREAM_MIME = 'video/mp4; codecs="avc1.42E01E"';

/**
 * Playback starts a few seconds behind the capture and, left alone, stays
 * there — a live view of the device would lag by that much forever. Past
 * ``MAX_LAG_S`` the playhead jumps back to the live edge, keeping
 * ``TARGET_LAG_S`` of cushion so it doesn't stall on the next frame.
 */
const MAX_LAG_S = 1.5;
const TARGET_LAG_S = 0.4;
/** Buffered video older than this is dropped, so a long session can't grow forever. */
const KEEP_BEHIND_S = 20;

type Health = "connecting" | "live" | "empty" | "error";
/**
 * How the live view is fed. ``video`` streams the Simulator window's own
 * buffer, so it survives the window being covered or moved; ``frames`` polls
 * screenshots and is the fallback whenever the stream can't start.
 */
type Feed = "video" | "frames";

interface BootedDevice {
  udid?: string;
  name?: string;
}

/**
 * Hold playback at the live edge and drop what has already been watched.
 *
 * The decoder starts a few seconds behind the capture and would keep that
 * distance for the whole session, so the "live" view would show the past.
 */
function keepAtLiveEdge(video: HTMLVideoElement, buffer: SourceBuffer) {
  const ranges = buffer.buffered;
  if (!ranges.length) return;
  const end = ranges.end(ranges.length - 1);
  if (end - video.currentTime > MAX_LAG_S) {
    video.currentTime = Math.max(0, end - TARGET_LAG_S);
  }
  // Evicting is only legal between appends, and only behind the playhead.
  const start = ranges.start(0);
  if (!buffer.updating && video.currentTime - start > KEEP_BEHIND_S) {
    try {
      buffer.remove(start, video.currentTime - KEEP_BEHIND_S);
    } catch {
      // A racing append makes this throw; the next chunk retries.
    }
  }
}

export interface SimulatorPaneProps {
  conversationId: string;
  /** Collapse the rail (the pane's own exit control). */
  onClose?: () => void;
  className?: string;
}

/**
 * The "iOS Simulator" workspace pane — a live view of the simulator on the
 * runner's Mac. The agent drives the device through the `ios_simulator` tool;
 * this pane is the window onto it: it streams real H.264 video of the device
 * (falling back to polling screenshots when the host can't screen-capture),
 * shows which device is booted, and forwards click-to-tap. When nothing is
 * booted it rests on an empty state rather than a broken image.
 */
export function SimulatorPane({ conversationId, onClose, className }: SimulatorPaneProps) {
  const [health, setHealth] = useState<Health>("connecting");
  const [streaming, setStreaming] = useState(true);
  const [device, setDevice] = useState<BootedDevice | null>(null);
  // Video by default: the stream reads the Simulator window's own buffer, so
  // it stays correct with the window covered or moved. Frames remain the
  // fallback for hosts that can't capture at all.
  const [feed, setFeed] = useState<Feed>(() =>
    typeof window !== "undefined" && window.MediaSource?.isTypeSupported?.(STREAM_MIME) === true
      ? "video"
      : "frames",
  );
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Device screenshot size, reported by the stream route (see the tap mapping).
  const shotSizeRef = useRef<[number, number] | null>(null);
  // Frame aspect ratio — seeded to the iPhone Pro screen and refined from the
  // first real frame so an iPad (or any device) isn't squeezed into a phone.
  const [aspect, setAspect] = useState("1206 / 2622");
  const imgRef = useRef<HTMLImageElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const inFlight = useRef(false);

  // The agent driving a turn == "Claude is using this device".
  const agentActive = useChatStore((s) => s.status === "streaming");

  const base = `/v1/sessions/${encodeURIComponent(conversationId)}/ios`;

  const refreshDevices = useCallback(async () => {
    try {
      const res = await hostFetch(`${base}/devices`);
      if (!res.ok) return;
      const data = (await res.json()) as { booted?: BootedDevice | null };
      setDevice(data.booted ?? null);
    } catch {
      // Non-fatal — the frame poll owns the health state.
    }
  }, [base]);

  const pullFrame = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const res = await hostFetch(`${base}/screenshot`);
      if (res.status === 409) {
        setHealth("empty");
        return;
      }
      if (!res.ok) {
        setHealth("error");
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = url;
      if (imgRef.current) imgRef.current.src = url;
      setHealth("live");
    } catch {
      setHealth("error");
    } finally {
      inFlight.current = false;
    }
  }, [base]);

  useEffect(() => {
    void refreshDevices();
  }, [refreshDevices]);

  useEffect(() => {
    if (!streaming || feed !== "frames") return;
    void pullFrame();
    const id = window.setInterval(() => void pullFrame(), FRAME_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [streaming, feed, pullFrame]);

  // Pump the fragmented-MP4 stream into a MediaSource. The <video> can't fetch
  // it directly — hostFetch carries the auth the route requires — so the bytes
  // are read here and appended by hand.
  useEffect(() => {
    if (!streaming || feed !== "video") return;
    const video = videoRef.current;
    if (!video) return;
    const abort = new AbortController();
    abortRef.current = abort;
    const mediaSource = new MediaSource();
    const src = URL.createObjectURL(mediaSource);
    video.src = src;
    let cancelled = false;

    const fallback = () => {
      if (!cancelled) setFeed("frames");
    };

    const onOpen = async () => {
      let buffer: SourceBuffer;
      try {
        buffer = mediaSource.addSourceBuffer(STREAM_MIME);
      } catch {
        fallback();
        return;
      }
      try {
        const res = await hostFetch(`${base}/stream`, { signal: abort.signal });
        if (res.status === 409 || !res.ok || !res.body) {
          // 409 means the host can't screen-capture — frames still work.
          fallback();
          return;
        }
        // Taps are addressed in screenshot pixels, which the cropped video
        // doesn't share — the route reports them so a click can be converted.
        const dims = res.headers.get("X-Device-Screenshot");
        const parsed = dims?.match(/^(\d+)x(\d+)$/);
        if (parsed) shotSizeRef.current = [Number(parsed[1]), Number(parsed[2])];
        const reader = res.body.getReader();
        setHealth("live");
        for (;;) {
          const { done, value } = await reader.read();
          if (done) {
            // The host stopped capturing (the window left the screen). Without
            // this the last frame would sit there looking live.
            fallback();
            return;
          }
          if (cancelled) break;
          // Appends must be serialized; wait out the previous one.
          if (buffer.updating) {
            await new Promise((r) => buffer.addEventListener("updateend", r, { once: true }));
          }
          if (cancelled || mediaSource.readyState !== "open") break;
          buffer.appendBuffer(value);
          keepAtLiveEdge(video, buffer);
        }
      } catch {
        if (!cancelled) fallback();
      }
    };

    mediaSource.addEventListener("sourceopen", () => void onOpen(), { once: true });
    void video.play().catch(() => {
      /* autoplay is muted, so a rejection here is benign */
    });

    return () => {
      cancelled = true;
      abort.abort();
      URL.revokeObjectURL(src);
      video.removeAttribute("src");
      video.load();
    };
  }, [streaming, feed, base]);

  useEffect(
    () => () => {
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    },
    [],
  );

  const handleReload = useCallback(() => {
    setHealth("connecting");
    void refreshDevices();
    void pullFrame();
  }, [refreshDevices, pullFrame]);

  const handleDownload = useCallback(() => {
    const url = objectUrlRef.current;
    if (!url) return;
    const a = document.createElement("a");
    a.href = url;
    a.download = `simulator-${Date.now()}.png`;
    a.click();
  }, []);

  const handleTap = useCallback(
    async (e: React.MouseEvent<HTMLImageElement | HTMLVideoElement>) => {
      const el = feed === "video" ? videoRef.current : imgRef.current;
      if (!el) return;
      // Both feeds show the same screen, but at different resolutions — the
      // tap endpoint speaks screenshot pixels, so convert through the fraction.
      const natural: [number, number] | null =
        feed === "video"
          ? (shotSizeRef.current ??
            (videoRef.current?.videoWidth
              ? [videoRef.current.videoWidth, videoRef.current.videoHeight]
              : null))
          : imgRef.current?.naturalWidth
            ? [imgRef.current.naturalWidth, imgRef.current.naturalHeight]
            : null;
      if (!natural) return;
      const rect = el.getBoundingClientRect();
      const x = Math.round(((e.clientX - rect.left) / rect.width) * natural[0]);
      const y = Math.round(((e.clientY - rect.top) / rect.height) * natural[1]);
      try {
        await hostFetch(`${base}/tap`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ x, y }),
        });
      } catch {
        // Touch injection can fail on the host; the tool surfaces why, not here.
      }
      // The video feed shows the result on its own; only frames need a nudge.
      if (feed === "frames") void pullFrame();
    },
    [base, feed, pullFrame],
  );

  const handleVideoLoad = useCallback(() => {
    const video = videoRef.current;
    if (video?.videoWidth && video.videoHeight) {
      setAspect(`${video.videoWidth} / ${video.videoHeight}`);
      setHealth("live");
    }
  }, []);

  const handleImgLoad = useCallback(() => {
    const img = imgRef.current;
    if (img?.naturalWidth && img.naturalHeight) {
      setAspect(`${img.naturalWidth} / ${img.naturalHeight}`);
    }
  }, []);

  // The tab only mounts in the desktop shell, but guard for correctness.
  const desktop = isElectronShell();

  return (
    <div className={cn("flex h-full flex-col bg-background", className)}>
      <header className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Smartphone className="size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0">
            <div className="truncate text-sm font-medium">{device?.name ?? "iOS Simulator"}</div>
            <div className="truncate text-[11px] text-muted-foreground">
              {device ? "Simulator" : "sem dispositivo"} ·{" "}
              {feed === "video" ? "vídeo H.264 30fps" : "quadros ~1fps"}
            </div>
          </div>
        </div>
        {agentActive && health === "live" ? (
          <span className="flex shrink-0 items-center gap-1.5 rounded-full bg-foreground px-2 py-1 text-[11px] font-medium text-background">
            <span className="size-1.5 rounded-full bg-red-500" />
            Claude usando o device
          </span>
        ) : null}
      </header>

      <div className="relative flex flex-1 items-center justify-center overflow-hidden bg-muted/40 p-4">
        {/* A phone-shaped chassis around the screen, so the live view reads as a
            real device rather than a floating rectangle. */}
        <div className="relative mx-auto h-full max-w-full" style={{ aspectRatio: aspect }}>
          <div
            className="relative h-full w-full bg-gradient-to-b from-neutral-700 via-neutral-900 to-black p-[3.2%] shadow-2xl ring-1 ring-black/60"
            style={{ borderRadius: "17% / 8%" }}
          >
            <div
              className="relative h-full w-full overflow-hidden bg-black"
              style={{ borderRadius: "14% / 6.6%" }}
            >
              {feed === "video" ? (
                <video
                  ref={videoRef}
                  muted
                  playsInline
                  autoPlay
                  aria-label="Tela do simulador iOS"
                  onClick={handleTap}
                  onLoadedMetadata={handleVideoLoad}
                  className={cn(
                    "h-full w-full object-cover transition-opacity duration-200",
                    health === "live" ? "cursor-pointer opacity-100" : "opacity-0",
                  )}
                />
              ) : (
                <img
                  ref={imgRef}
                  alt="Tela do simulador iOS"
                  onClick={handleTap}
                  onLoad={handleImgLoad}
                  className={cn(
                    "h-full w-full object-cover transition-opacity duration-200",
                    health === "live" ? "cursor-pointer opacity-100" : "opacity-0",
                  )}
                />
              )}
              {/* Dynamic Island */}
              <div className="pointer-events-none absolute left-1/2 top-[1.4%] h-[3.4%] w-[30%] -translate-x-1/2 rounded-full bg-black" />
              {health !== "live" ? (
                <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-2 px-6 text-center text-xs text-neutral-400">
                  {health === "connecting" ? (
                    <Loader2 className="size-5 animate-spin" />
                  ) : (
                    <Smartphone className="size-7 opacity-40" />
                  )}
                  <p>{statusMessage(health, desktop)}</p>
                </div>
              ) : null}
            </div>
          </div>
          {/* Side buttons — action/mute + volume on the left, power on the right. */}
          <span className="absolute -left-[3px] top-[16%] h-[5%] w-[3px] rounded-l bg-neutral-700" />
          <span className="absolute -left-[3px] top-[27%] h-[9%] w-[3px] rounded-l bg-neutral-700" />
          <span className="absolute -left-[3px] top-[39%] h-[9%] w-[3px] rounded-l bg-neutral-700" />
          <span className="absolute -right-[3px] top-[32%] h-[12%] w-[3px] rounded-r bg-neutral-700" />
        </div>
      </div>

      <footer className="flex items-center justify-center gap-1 border-t border-border px-3 py-2">
        <ControlButton
          label={streaming ? "Pausar" : "Retomar"}
          onClick={() => setStreaming((s) => !s)}
        >
          {streaming ? <Pause className="size-4" /> : <Play className="size-4" />}
        </ControlButton>
        <ControlButton
          label={feed === "video" ? "Usar quadros" : "Usar vídeo"}
          onClick={() => {
            setHealth("connecting");
            setFeed((f) => (f === "video" ? "frames" : "video"));
          }}
        >
          <Video className="size-4" />
        </ControlButton>
        <ControlButton label="Capturar" onClick={handleDownload} disabled={health !== "live"}>
          <Camera className="size-4" />
        </ControlButton>
        <ControlButton label="Recarregar" onClick={handleReload}>
          <RotateCw className="size-4" />
        </ControlButton>
        {onClose ? (
          <ControlButton label="Fechar" onClick={onClose}>
            <X className="size-4" />
          </ControlButton>
        ) : null}
      </footer>
    </div>
  );
}

function statusMessage(health: Health, desktop: boolean): string {
  if (!desktop) {
    return "O simulador roda na máquina com Xcode — abra o OmniCraft no desktop para ver a tela.";
  }
  if (health === "empty") {
    return "Nenhum simulador em execução. Peça ao agente para bootar um (ios_simulator: boot).";
  }
  if (health === "error") {
    return "Não consegui capturar a tela do simulador.";
  }
  return "Conectando ao simulador…";
}

function ControlButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className="inline-flex size-9 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
    >
      {children}
    </button>
  );
}
