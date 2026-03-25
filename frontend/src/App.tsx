import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";

type Danger = "low" | "medium" | "high";

type Analysis = {
  success: boolean;
  object: string;
  problem: string;
  danger_level: Danger;
  warnings: string[];
  tools_needed: string[];
  steps: string[];
  extra_tips: string[];
  demo_mode: boolean;
};

type Phase = "camera" | "loading" | "safety" | "steps" | "celebrate" | "signup" | "wrap";

const analyzeUrl = import.meta.env.VITE_API_URL
  ? `${import.meta.env.VITE_API_URL.replace(/\/$/, "")}/analyze`
  : "/analyze";

const collectEmailUrl = import.meta.env.VITE_API_URL
  ? `${import.meta.env.VITE_API_URL.replace(/\/$/, "")}/collect-email`
  : "/collect-email";

/** Skip signup next time after user submits or taps Skip */
const LS_SIGNUP_DONE = "ce_mvp_signup_done";

function playTone(freq: number, dur: number, vol = 0.06) {
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = "sine";
    o.frequency.value = freq;
    g.gain.setValueAtTime(vol, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + dur);
    o.connect(g);
    g.connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + dur);
    setTimeout(() => ctx.close(), (dur + 0.1) * 1000);
  } catch {
    /* optional */
  }
}

function playScan() {
  playTone(440, 0.08, 0.05);
  setTimeout(() => playTone(660, 0.1, 0.04), 70);
}

function playSuccess() {
  playTone(523, 0.1, 0.055);
  setTimeout(() => playTone(784, 0.12, 0.05), 100);
}

export default function App() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const streamRef = useRef<MediaStream | null>(null);

  const [phase, setPhase] = useState<Phase>("camera");
  const [camError, setCamError] = useState<string | null>(null);
  const [camFacing, setCamFacing] = useState<"environment" | "user">("environment");
  const [data, setData] = useState<Analysis | null>(null);
  const [stepIdx, setStepIdx] = useState(0);
  const [signupEmail, setSignupEmail] = useState("");
  const [signupErr, setSignupErr] = useState("");

  const startCamera = useCallback(async () => {
    setCamError(null);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: camFacing, width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
      streamRef.current = stream;
      const v = videoRef.current;
      if (v) {
        v.srcObject = stream;
        await v.play();
      }
    } catch {
      setCamError("Camera off — tap Memories to pick a photo.");
    }
  }, [camFacing]);

  useEffect(() => {
    void startCamera();
    return () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, [startCamera]);

  useEffect(() => {
    if (phase !== "signup") return;
    const id = window.setTimeout(() => document.getElementById("mvp-email")?.focus({ preventScroll: true }), 380);
    return () => clearTimeout(id);
  }, [phase]);

  const captureBlob = useCallback((): Promise<Blob> => {
    return new Promise((resolve, reject) => {
      const v = videoRef.current;
      if (!v || v.videoWidth < 2) {
        reject(new Error("No video"));
        return;
      }
      const c = document.createElement("canvas");
      c.width = v.videoWidth;
      c.height = v.videoHeight;
      const ctx = c.getContext("2d");
      if (!ctx) {
        reject(new Error("Canvas"));
        return;
      }
      ctx.drawImage(v, 0, 0);
      c.toBlob(
        (b) => {
          if (b) resolve(b);
          else reject(new Error("Blob"));
        },
        "image/jpeg",
        0.88
      );
    });
  }, []);

  const runAnalyze = useCallback(async (blob: Blob) => {
    setPhase("loading");
    playScan();
    const fd = new FormData();
    fd.append("file", blob, "scan.jpg");
    try {
      const r = await fetch(analyzeUrl, { method: "POST", body: fd });
      if (!r.ok) throw new Error(`Server ${r.status}`);
      const json = (await r.json()) as Analysis;
      if (!json.steps?.length) throw new Error("No steps");
      setData(json);
      setStepIdx(0);
      playSuccess();
      const d = (json.danger_level || "low").toLowerCase() as Danger;
      if (d === "medium" || d === "high") setPhase("safety");
      else setPhase("steps");
    } catch {
      const fallback: Analysis = {
        success: true,
        object: "Air fryer (basket style)",
        problem: "Greasy buildup — quick clean restores airflow and taste.",
        danger_level: "low",
        warnings: ["Unplug and cool completely before cleaning."],
        tools_needed: ["Soft sponge", "Dish soap", "Microfiber cloth"],
        steps: [
          "Unplug and let the basket cool.",
          "Remove basket; shake out crumbs.",
          "Wash basket in warm soapy water; no abrasive pads on non-stick.",
          "Wipe the chamber with a barely-damp cloth away from the element.",
          "Dry fully, reassemble, run empty 2 min to finish.",
        ],
        extra_tips: ["Soak 10 min if grease is stuck.", "Demo mode — connect API for live vision."],
        demo_mode: true,
      };
      setData(fallback);
      setStepIdx(0);
      setPhase("steps");
    }
  }, []);

  const onOrbPress = async () => {
    if (phase === "loading") return;
    try {
      const blob = await captureBlob();
      await runAnalyze(blob);
    } catch {
      setCamError("Capture failed — try upload.");
      setPhase("camera");
    }
  };

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    await runAnalyze(f);
  };

  const dangerOn = Boolean(
    data &&
      (data.danger_level === "medium" || data.danger_level === "high") &&
      (phase === "safety" || phase === "steps" || phase === "wrap")
  );

  const continueFromCelebrate = () => {
    if (typeof localStorage !== "undefined" && localStorage.getItem(LS_SIGNUP_DONE)) {
      setPhase("wrap");
      return;
    }
    setPhase("signup");
    setSignupEmail("");
    setSignupErr("");
  };

  const skipSignup = () => {
    try {
      localStorage.setItem(LS_SIGNUP_DONE, "skip");
    } catch {
      /* ignore */
    }
    setPhase("wrap");
  };

  const submitSignup = async () => {
    const em = signupEmail.trim();
    if (!em || !em.includes("@")) {
      setSignupErr("Enter a valid email");
      return;
    }
    setSignupErr("");
    try {
      await fetch(collectEmailUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: em, source: "mvp-you-did-it", language: "en" }),
      });
    } catch {
      /* still thank — demo resilience */
    }
    try {
      localStorage.setItem(LS_SIGNUP_DONE, "1");
    } catch {
      /* ignore */
    }
    setPhase("wrap");
  };

  const steps = data?.steps ?? [];
  const lastStep = stepIdx >= steps.length - 1;

  return (
    <div className="app">
      <div className={`danger-veil ${dangerOn ? "on" : ""}`} aria-hidden />

      <input ref={fileRef} type="file" accept="image/*" capture="environment" style={{ display: "none" }} onChange={onFile} />

      {phase === "camera" || phase === "loading" ? (
        <>
          <div className="cam-layer">
            {!camError ? (
              <video ref={videoRef} playsInline muted autoPlay />
            ) : (
              <div className="cam-fallback">
                <p>{camError}</p>
                <button type="button" className="btn-snap-primary" onClick={() => fileRef.current?.click()}>
                  Open Memories
                </button>
              </div>
            )}
            <div className="cam-fade-top" />
            <div className="cam-fade-bottom" />
            <div className="cam-vignette cam-vignette--snap" />
          </div>

          <header className="snap-top">
            <div className="snap-brand-chip">
              <span className="snap-egg" aria-hidden>
                🥚
              </span>
              <span className="snap-brand-txt">Chicken Egg</span>
            </div>
          </header>

          <div className={`scan-fx ${phase === "loading" ? "" : "off"}`}>
            <div className="wave wave--snap" />
            <div className="wave wave--snap" />
            <div className="wave wave--snap" />
          </div>

          {phase === "loading" && (
            <div className="thinking thinking--snap">
              <span className="thinking-dot" />
              Scanning
              <span className="thinking-dots">...</span>
            </div>
          )}

          {!camError && (
            <nav className="snap-dock" aria-label="Camera controls">
              <button
                type="button"
                className="snap-side-btn snap-memories"
                aria-label="Memories — choose photo"
                onClick={() => fileRef.current?.click()}
              >
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden>
                  <rect x="3" y="3" width="8" height="8" rx="2" fill="currentColor" opacity="0.95" />
                  <rect x="13" y="3" width="8" height="8" rx="2" fill="currentColor" opacity="0.55" />
                  <rect x="3" y="13" width="8" height="8" rx="2" fill="currentColor" opacity="0.55" />
                  <rect x="13" y="13" width="8" height="8" rx="2" fill="currentColor" opacity="0.95" />
                </svg>
              </button>

              <div className="snap-shutter-wrap">
                <span className="snap-hint">{phase === "loading" ? "Hold tight" : "Tap"}</span>
                <button
                  type="button"
                  className={`snap-shutter ${phase === "loading" ? "snap-shutter--busy" : ""}`}
                  disabled={phase === "loading"}
                  onClick={() => void onOrbPress()}
                  aria-label="Capture and analyze"
                >
                  <span className="snap-shutter-lens" />
                </button>
              </div>

              <button
                type="button"
                className="snap-side-btn snap-flip"
                aria-label="Flip camera"
                onClick={() => setCamFacing((f) => (f === "environment" ? "user" : "environment"))}
              >
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                  <path
                    d="M20 10c-1.2-4-5-6-9-4.5M4 14c1.2 4 5 6 9 4.5"
                    strokeLinecap="round"
                  />
                  <path d="M4 10V6h4M20 14v4h-4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </nav>
          )}
        </>
      ) : null}

      {phase === "celebrate" && data ? (
        <div className="mvp-celebrate" role="dialog" aria-labelledby="mvp-celebrate-ttl" aria-modal="true">
          <div className="mvp-celebrate-burst" aria-hidden />
          <div className="mvp-celebrate-card">
            <div className="mvp-celebrate-ico" aria-hidden>
              🏆
            </div>
            <h2 id="mvp-celebrate-ttl" className="mvp-celebrate-ttl">
              You did it!
            </h2>
            <p className="mvp-celebrate-sub">That fix is yours. One more tap to lock in early access.</p>
            <button type="button" className="btn-snap-primary mvp-celebrate-btn" onClick={continueFromCelebrate}>
              Continue
            </button>
          </div>
        </div>
      ) : null}

      {phase === "signup" && data ? (
        <div className="mvp-signup-root" role="dialog" aria-labelledby="mvp-signup-ttl" aria-modal="true">
          <button type="button" className="mvp-signup-scrim" aria-label="Close" onClick={skipSignup} />
          <div className="mvp-signup-sheet">
            <div className="sheet-handle" aria-hidden />
            <h2 id="mvp-signup-ttl" className="mvp-signup-ttl">
              Get the good stuff first
            </h2>
            <p className="mvp-signup-sub">Drop your email — we&apos;ll send product drops and founder updates. No spam.</p>
            <label className="mvp-signup-label" htmlFor="mvp-email">
              Email
            </label>
            <input
              id="mvp-email"
              className="mvp-signup-input"
              type="email"
              autoComplete="email"
              placeholder="you@email.com"
              value={signupEmail}
              onChange={(e) => {
                setSignupEmail(e.target.value);
                setSignupErr("");
              }}
            />
            {signupErr ? <p className="mvp-signup-err">{signupErr}</p> : null}
            <button type="button" className="btn-snap-primary mvp-signup-submit" onClick={() => void submitSignup()}>
              Count me in
            </button>
            <button type="button" className="mvp-signup-skip" onClick={skipSignup}>
              Not now
            </button>
          </div>
        </div>
      ) : null}

      {data && (phase === "safety" || phase === "steps" || phase === "wrap") ? (
        <div className="sheet sheet--snap">
          <div className="sheet-handle" aria-hidden />
          <div className="sheet-inner">
            <div className="sheet-header">
              <div className="sheet-kicker">Identified</div>
              <h1 className="sheet-object">{data.object}</h1>
              <p className="sheet-problem">{data.problem}</p>
            </div>

            {data.demo_mode ? <div className="demo-pill">Demo mode</div> : <div className="demo-pill off" />}

            {phase === "safety" ? (
              <>
                <div className="warn-block">
                  <h3>Safety first</h3>
                  <ul>
                    {(data.warnings.length ? data.warnings : ["Use common sense; stop if anything feels unsafe."]).map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                </div>
                <div className="cta-row">
                  <button type="button" className="btn-primary" onClick={() => setPhase("steps")}>
                    Continue to steps
                  </button>
                </div>
              </>
            ) : null}

            {phase === "steps" ? (
              <>
                {stepIdx === 0 && data.tools_needed.length > 0 ? (
                  <div className="tools-strip">
                    {data.tools_needed.map((t) => (
                      <span key={t} className="tool-chip">
                        {t}
                      </span>
                    ))}
                  </div>
                ) : null}

                <div className="step-card">
                  <div className="step-meta">
                    Step {stepIdx + 1} / {steps.length}
                  </div>
                  <div className="step-text" key={stepIdx}>
                    {steps[stepIdx]}
                  </div>
                  <div className="dots" aria-hidden>
                    {steps.map((_, i) => (
                      <span key={i} className={`dot ${i === stepIdx ? "on" : ""}`} />
                    ))}
                  </div>
                </div>

                <div className="cta-row">
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => {
                      if (lastStep) {
                        playSuccess();
                        setPhase("celebrate");
                      } else setStepIdx((i) => i + 1);
                    }}
                  >
                    {lastStep ? "Finish" : "Next step"}
                  </button>
                  <button
                    type="button"
                    className="btn-ghost"
                    onClick={() => {
                      setPhase("camera");
                      setData(null);
                      setStepIdx(0);
                    }}
                  >
                    New scan
                  </button>
                </div>
              </>
            ) : null}

            {phase === "wrap" ? (
              <>
                {data.extra_tips.length > 0 ? (
                  <div className="tips-block">
                    <h4>Pro tips</h4>
                    <ul>
                      {data.extra_tips.map((t) => (
                        <li key={t}>{t}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                <div className="cta-row">
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => {
                      setPhase("camera");
                      setData(null);
                      setStepIdx(0);
                      void startCamera();
                    }}
                  >
                    Scan something else
                  </button>
                </div>
              </>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
