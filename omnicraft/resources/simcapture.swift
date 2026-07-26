// Capture a single window's own buffer and write raw BGRA frames to stdout.
//
// The iOS Simulator pane needs live video of the device. Capturing a *region*
// of the screen fails in the normal case: the app window showing the pane sits
// on top of the Simulator, so the capture mirrors the app instead of the
// device, and a fixed crop stops matching once the window moves.
//
// ScreenCaptureKit hands over the window's own composited buffer, so the
// window may be occluded, moved, or resized and the frames stay correct.
// Encoding is left to ffmpeg downstream — this only produces frames.
//
// Usage: simcapture <owner-app-name> [fps] [max-width]
// Prints "WIDTHxHEIGHT" and a newline to stderr, then streams BGRA to stdout.

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

/// Exit codes the Python caller distinguishes.
enum ExitCode: Int32 {
    case badArguments = 2
    case noWindow = 3
    case captureFailed = 4
}

func fail(_ message: String, _ code: ExitCode) -> Never {
    FileHandle.standardError.write(Data("simcapture: \(message)\n".utf8))
    exit(code.rawValue)
}

/// Holds the newest frame and writes it to stdout at a steady cadence.
///
/// ScreenCaptureKit only delivers a frame when the window's content changes,
/// so a still device would emit almost nothing — while raw video downstream
/// assumes a constant rate and would play back at the wrong speed. A ticker
/// republishing the latest frame keeps the timeline honest.
final class FrameWriter: NSObject, SCStreamOutput {
    private let width: Int
    private let height: Int
    private let out = FileHandle.standardOutput
    private let lock = NSLock()
    private var latest: Data?
    private var ticker: DispatchSourceTimer?

    init(width: Int, height: Int) {
        self.width = width
        self.height = height
    }

    /// Begin emitting at *fps*, repeating the last frame while nothing changes.
    func start(fps: Int) {
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "ai.omnicraft.simcapture.out"))
        timer.schedule(deadline: .now(), repeating: .milliseconds(1000 / max(1, fps)))
        timer.setEventHandler { [weak self] in self?.emit() }
        timer.resume()
        ticker = timer
    }

    private func emit() {
        lock.lock()
        let frame = latest
        lock.unlock()
        guard let frame else { return }  // nothing captured yet
        // A closed pipe (the client hung up) surfaces as a write failure — the
        // process should end rather than spin capturing frames nobody reads.
        do {
            try out.write(contentsOf: frame)
        } catch {
            exit(0)
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, CMSampleBufferIsValid(buffer),
              let pixels = CMSampleBufferGetImageBuffer(buffer) else { return }
        CVPixelBufferLockBaseAddress(pixels, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(pixels) else { return }

        let stride = CVPixelBufferGetBytesPerRow(pixels)
        let rowBytes = width * 4
        let rows = min(height, CVPixelBufferGetHeight(pixels))
        var frame = Data(capacity: rowBytes * rows)
        // Rows are padded to the hardware's alignment; copy only the visible
        // bytes so the raw stream matches the size ffmpeg is told to expect.
        for row in 0..<rows {
            frame.append(Data(bytes: base.advanced(by: row * stride), count: rowBytes))
        }
        lock.lock()
        latest = frame
        lock.unlock()
    }
}

/// Find the largest on-screen window belonging to *owner*, occluded or not.
func findWindow(owner: String) async throws -> SCWindow {
    let content = try await SCShareableContent.excludingDesktopWindows(
        false, onScreenWindowsOnly: false
    )
    let candidates = content.windows.filter { window in
        window.owningApplication?.applicationName == owner && window.frame.width > 1
    }
    guard let window = candidates.max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height })
    else {
        fail("no window owned by \(owner)", .noWindow)
    }
    return window
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    fail("usage: simcapture <owner-app-name> [fps]", .badArguments)
}
let owner = args[1]
let fps = args.count > 2 ? Int(args[2]) ?? 30 : 30
// Raw BGRA is ~7MB a frame at Retina size; the pane draws the phone small, so
// capping the width keeps the pipe (and the encoder) from doing pointless work.
let maxWidth = args.count > 3 ? Int(args[3]) ?? 640 : 640

// Held for the process's lifetime: the stream keeps no strong reference to its
// output, so letting these go out of scope silently stops the capture.
var liveStream: SCStream?
var liveWriter: FrameWriter?

let semaphore = DispatchSemaphore(value: 0)
Task {
    do {
        let window = try await findWindow(owner: owner)
        // The buffer is in pixels; the frame is in points, so scale up for a
        // Retina display or the capture comes out soft and half-size — then
        // clamp so a large window doesn't flood the pipe.
        let backing = NSScreen.main.map { $0.backingScaleFactor } ?? 2.0
        let scale = min(backing, Double(maxWidth) / window.frame.width)
        // H.264 needs even dimensions.
        let width = Int(window.frame.width * scale) / 2 * 2
        let height = Int(window.frame.height * scale) / 2 * 2

        let config = SCStreamConfiguration()
        config.width = width
        config.height = height
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
        config.showsCursor = false
        config.queueDepth = 3

        let filter = SCContentFilter(desktopIndependentWindow: window)
        let stream = SCStream(filter: filter, configuration: config, delegate: nil)
        let writer = FrameWriter(width: width, height: height)
        try stream.addStreamOutput(
            writer, type: .screen, sampleHandlerQueue: DispatchQueue(label: "ai.omnicraft.simcapture")
        )
        // The caller reads this to tell ffmpeg the exact frame geometry.
        FileHandle.standardError.write(Data("\(width)x\(height)\n".utf8))
        liveStream = stream
        liveWriter = writer
        try await stream.startCapture()
        writer.start(fps: fps)
    } catch {
        fail("capture failed: \(error.localizedDescription)", .captureFailed)
    }
}
semaphore.wait()
