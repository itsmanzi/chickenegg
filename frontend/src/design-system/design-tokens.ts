/* Design Tokens – Premium color, typography, spacing, shadows, and animation */

export const colors = {
  bg: {
    base: "#0D1117",
    surface: "#11161E",
    elevated: "#1A2034",
  },
  text: {
    primary: "#E8E8E8",
    secondary: "#A8A8A8",
    tertiary: "#707070",
  },
  accent: {
    metallic: "#C9AC7F", // Refined gold
  },
  semantic: {
    success: "#10B981",
    warning: "#F59E0B",
    danger: "#EF4444",
  },
};

export const typography = {
  h1: {
    fontSize: "28px",
    fontWeight: 600,
    lineHeight: "1.2",
    letterSpacing: "-0.02em",
  },
  h2: {
    fontSize: "22px",
    fontWeight: 500,
    lineHeight: "1.3",
    letterSpacing: "-0.01em",
  },
  body: {
    fontSize: "16px",
    fontWeight: 400,
    lineHeight: "1.6",
    letterSpacing: "0em",
  },
  small: {
    fontSize: "14px",
    fontWeight: 400,
    lineHeight: "1.5",
    letterSpacing: "0em",
  },
  xs: {
    fontSize: "12px",
    fontWeight: 500,
    lineHeight: "1.4",
    letterSpacing: "0.05em",
  },
};

export const spacing = {
  xs: "8px",
  sm: "12px",
  md: "16px",
  lg: "24px",
  xl: "32px",
  xxl: "48px",
};

export const shadows = {
  sm: "0 2px 8px rgba(0, 0, 0, 0.12)",
  md: "0 4px 16px rgba(0, 0, 0, 0.16)",
  lg: "0 8px 32px rgba(0, 0, 0, 0.2)",
  xl: "0 16px 48px rgba(0, 0, 0, 0.24)",
};

export const animations = {
  duration: {
    fast: "200ms",
    standard: "300ms",
    slow: "500ms",
  },
  easing: {
    smooth: "cubic-bezier(0.4, 0, 0.2, 1)",
  },
};

export const borderRadius = {
  sm: "8px",
  md: "12px",
  lg: "16px",
  full: "9999px",
};
