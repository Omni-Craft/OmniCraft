import { useCallback, useEffect, useRef, useState } from "react";
import { Camera, Loader2, Pause, Play, RotateCw, Smartphone, X } from "lucide-react";

import { hostFetch } from "@/lib/host";
import { isElectronShell } from "@/lib/nativeBridge";
import { useChatStore } from "@/store/chatStore";
import { cn } from "@/lib/utils";

/** How often the live view pulls a fresh frame (simctl screenshot ~3-5 fps). */
const FRAME_INTERVAL_MS = 800;

type Health = "connecting" | "live" | "empty" | "error";

interface BootedDevice {
  udid?: string;
  name?: string;
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
 * this pane is the window onto it: it polls the server for a fresh screenshot
 * (the closest thing to a stream without an encoder pipeline), shows which
 * device is booted, and forwards click-to-tap. When nothing is booted it rests
 * on an empty state rather than a broken image.
 */
export function SimulatorPane({ conversationId, onClose, className }: SimulatorPaneProps) {
  const [health, setHealth] = useState<Health>("connecting");
  const [streaming, setStreaming] = useState(true);
  const [device, setDevice] = useState<BootedDevice | null>(null);
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
    if (!streaming) return;
    void pullFrame();
    const id = window.setInterval(() => void pullFrame(), FRAME_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [streaming, pullFrame]);

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
    async (e: React.MouseEvent<HTMLImageElement>) => {
      const img = imgRef.current;
      if (!img || !img.naturalWidth) return;
      const rect = img.getBoundingClientRect();
      // Map the click within the displayed image to device pixel coordinates.
      const x = Math.round(((e.clientX - rect.left) / rect.width) * img.naturalWidth);
      const y = Math.round(((e.clientY - rect.top) / rect.height) * img.naturalHeight);
      try {
        await hostFetch(`${base}/tap`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ x, y }),
        });
      } catch {
        // Tap needs idb on the host; failures are surfaced by the tool, not here.
      }
      void pullFrame();
    },
    [base, pullFrame],
  );

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
              {device ? "Simulator" : "sem dispositivo"} · 50% · H.264
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
