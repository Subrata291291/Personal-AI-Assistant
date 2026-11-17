export default function MyOrbitLogo({ className = 'h-6 w-6', title = 'MyOrbit AI' }) {
  return (
    <svg
      viewBox="0 0 48 48"
      className={className}
      role="img"
      aria-label={title}
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <linearGradient id="myorbitGrad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#8b5cf6" />
          <stop offset="55%" stopColor="#7c3aed" />
          <stop offset="100%" stopColor="#6366f1" />
        </linearGradient>
        <linearGradient id="myorbitRing" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stopColor="#a78bfa" stopOpacity="0.9" />
          <stop offset="100%" stopColor="#818cf8" stopOpacity="0.55" />
        </linearGradient>
      </defs>
      {/* Outer orbit ring */}
      <ellipse
        cx="24"
        cy="24"
        rx="20"
        ry="9.5"
        fill="none"
        stroke="url(#myorbitRing)"
        strokeWidth="1.8"
        transform="rotate(-18 24 24)"
      />
      {/* Inner orbit ring */}
      <ellipse
        cx="24"
        cy="24"
        rx="14.5"
        ry="6.5"
        fill="none"
        stroke="url(#myorbitRing)"
        strokeWidth="1.4"
        transform="rotate(24 24 24)"
        opacity="0.85"
      />
      {/* Orbit dot */}
      <circle cx="42.5" cy="18.5" r="2.4" fill="#c4b5fd" />
      {/* Central core */}
      <circle cx="24" cy="24" r="5.8" fill="url(#myorbitGrad)" />
      <circle cx="24" cy="24" r="2.5" fill="#ede9fe" opacity="0.95" />
    </svg>
  )
}
