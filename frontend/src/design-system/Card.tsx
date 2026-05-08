import React from "react";
import "./Card.css";

type CardVariant = "minimal" | "premium" | "glass";

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: CardVariant;
  children: React.ReactNode;
}

const Card = React.forwardRef<HTMLDivElement, CardProps>(
  ({ variant = "premium", children, className, ...props }, ref) => {
    const variantClass = `card--${variant}`;

    return (
      <div ref={ref} className={`card ${variantClass} ${className || ""}`} {...props}>
        {children}
      </div>
    );
  }
);

Card.displayName = "Card";

export default Card;
