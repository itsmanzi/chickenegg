import React from "react";
import { colors, animations, spacing, borderRadius } from "./design-tokens";
import "./Button.css";

type ButtonVariant = "primary" | "secondary" | "ghost";
type ButtonSize = "sm" | "md" | "lg";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  isLoading?: boolean;
  children: React.ReactNode;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = "primary", size = "md", isLoading = false, disabled, children, className, ...props }, ref) => {
    const variantClass = `btn--${variant}`;
    const sizeClass = `btn--${size}`;
    const loadingClass = isLoading ? "btn--loading" : "";
    const disabledClass = disabled || isLoading ? "btn--disabled" : "";

    return (
      <button
        ref={ref}
        className={`btn ${variantClass} ${sizeClass} ${loadingClass} ${disabledClass} ${className || ""}`}
        disabled={disabled || isLoading}
        {...props}
      >
        {isLoading && <span className="btn-spinner" aria-hidden />}
        <span className={isLoading ? "btn-text--hidden" : ""}>{children}</span>
      </button>
    );
  }
);

Button.displayName = "Button";

export default Button;
