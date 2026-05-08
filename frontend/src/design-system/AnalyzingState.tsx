import React from "react";
import "./AnalyzingState.css";

interface AnalyzingStateProps {
  message?: string;
  duration?: string;
}

const AnalyzingState: React.FC<AnalyzingStateProps> = ({
  message = "Analyzing",
  duration = "2.4s",
}) => {
  return (
    <div className="analyzing-state" role="status" aria-live="polite">
      <div className="analyzing-orb-wrapper">
        <svg
          className="analyzing-orb"
          viewBox="0 0 120 120"
          xmlns="http://www.w3.org/2000/svg"
          width="120"
          height="120"
          style={{ "--animation-duration": duration } as React.CSSProperties}
        >
          {/* Central beam circle */}
          <defs>
            <linearGradient id="beamGradient" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stopColor="#C9AC7F" stopOpacity="0.1" />
              <stop offset="50%" stopColor="#C9AC7F" stopOpacity="0.6" />
              <stop offset="100%" stopColor="#C9AC7F" stopOpacity="0.1" />
            </linearGradient>
          </defs>

          {/* Circle path for shimmer animation */}
          <circle cx="60" cy="60" r="45" fill="none" stroke="url(#beamGradient)" strokeWidth="2" />

          {/* Cardinal rays (4) */}
          <line x1="60" y1="15" x2="60" y2="30" stroke="#C9AC7F" strokeWidth="2" opacity="0.8" />
          <line x1="105" y1="60" x2="90" y2="60" stroke="#C9AC7F" strokeWidth="2" opacity="0.8" />
          <line x1="60" y1="105" x2="60" y2="90" stroke="#C9AC7F" strokeWidth="2" opacity="0.8" />
          <line x1="15" y1="60" x2="30" y2="60" stroke="#C9AC7F" strokeWidth="2" opacity="0.8" />

          {/* Diagonal rays (4) */}
          <line x1="82" y1="19" x2="73" y2="28" stroke="#C9AC7F" strokeWidth="2" opacity="0.5" />
          <line x1="101" y1="38" x2="92" y2="47" stroke="#C9AC7F" strokeWidth="2" opacity="0.5" />
          <line x1="101" y1="82" x2="92" y2="73" stroke="#C9AC7F" strokeWidth="2" opacity="0.5" />
          <line x1="82" y1="101" x2="73" y2="92" stroke="#C9AC7F" strokeWidth="2" opacity="0.5" />
          <line x1="38" y1="101" x2="47" y2="92" stroke="#C9AC7F" strokeWidth="2" opacity="0.5" />
          <line x1="19" y1="82" x2="28" y2="73" stroke="#C9AC7F" strokeWidth="2" opacity="0.5" />
          <line x1="19" y1="38" x2="28" y2="47" stroke="#C9AC7F" strokeWidth="2" opacity="0.5" />
          <line x1="38" y1="19" x2="47" y2="28" stroke="#C9AC7F" strokeWidth="2" opacity="0.5" />

          {/* Center orb */}
          <circle cx="60" cy="60" r="6" fill="#C9AC7F" opacity="0.9" />
        </svg>
      </div>

      <div className="analyzing-text">
        <p className="analyzing-message">{message}</p>
        <p className="analyzing-dots">
          <span>.</span>
          <span>.</span>
          <span>.</span>
        </p>
      </div>
    </div>
  );
};

export default AnalyzingState;
