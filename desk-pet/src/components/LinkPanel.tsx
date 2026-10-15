// First-run experience: create a link code and poll the API until the
// user runs /link-desktop in Discord. After the token lands we sign in
// and the panel unmounts.
//
// The server (base URL) is intentionally NOT shown here — it's fixed by
// the build / owner config (VITE_DESK_API_BASE, or the persisted
// base.txt). Regular members never need to see or change it.
import { useEffect, useState } from "react";
import { usePetStore } from "../store/petStore";

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    // Fallback for older/locked-down renderers without the async API.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch {
      /* ignore — nothing else we can do */
    }
    document.body.removeChild(ta);
  }
}

function Countdown({ expiresAt }: { expiresAt: string }): JSX.Element {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  const ms = new Date(expiresAt).getTime() - now;
  if (ms <= 0) {
    return <p className="error">⏰ Code đã hết hạn — bấm “Tạo code mới”.</p>;
  }
  const total = Math.floor(ms / 1000);
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return (
    <p className="muted">
      Hết hạn sau <strong>{mm}:{ss}</strong>
    </p>
  );
}

export default function LinkPanel(): JSX.Element {
  const link = usePetStore((s) => s.link);
  const beginLink = usePetStore((s) => s.beginLink);
  const cancelLink = usePetStore((s) => s.cancelLink);
  const [copied, setCopied] = useState(false);

  useEffect(() => () => cancelLink(), [cancelLink]);

  const onCopy = async (text: string) => {
    await copyText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="content">
      <button
        className="panel-close"
        title="Đóng (ẩn xuống tray)"
        aria-label="Đóng"
        onClick={() => void window.akimDeskPet?.window?.hide()}
      >
        ×
      </button>
      <h2>Liên kết Discord</h2>
      <p className="muted">
        App này hiển thị pet trong server Discord của bạn. Cần liên kết 1 lần
        đầu — sau đó token lưu trên máy.
      </p>

      <div style={{ height: 12 }} />

      {link.kind === "idle" && (
        <button className="primary" onClick={beginLink}>
          Tạo link code
        </button>
      )}
      {link.kind === "creating" && <p>Đang tạo code...</p>}
      {link.kind === "awaiting" && (
        <>
          <p className="muted">Vào Discord, dán lệnh này rồi gửi:</p>
          <button
            type="button"
            className="code-block code-copyable"
            onClick={() => onCopy(`/link-desktop code:${link.code}`)}
            title="Bấm để copy lệnh"
          >
            <span className="code-main">{link.code}</span>
            <span className="code-sub">/link-desktop code:{link.code}</span>
            <span className="code-copy-hint">
              {copied ? "✓ Đã copy lệnh!" : "📋 bấm để copy lệnh"}
            </span>
          </button>
          <Countdown expiresAt={link.expiresAt} />
          <button className="ghost" onClick={beginLink}>
            Tạo code mới
          </button>
        </>
      )}
      {link.kind === "linking" && <p>Đang ký token...</p>}
      {link.kind === "error" && (
        <>
          <p className="error">{link.message}</p>
          <button className="primary" onClick={beginLink}>
            Thử lại
          </button>
        </>
      )}
    </div>
  );
}
