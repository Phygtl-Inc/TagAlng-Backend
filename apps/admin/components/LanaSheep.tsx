'use client';

type Mood = 'idle' | 'listening' | 'thinking' | 'noticing';

type Props = {
  mood?: Mood;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
};

/**
 * Lana mascot — fluffy cream wool, pink muzzle, black shades, four legs.
 * Matches PWA mockup (wool dominant; face + glasses sit low on the puff).
 */
export function LanaSheep({ mood = 'idle', size = 'lg', className = '' }: Props) {
  const scale = size === 'sm' ? 0.55 : size === 'md' ? 0.78 : 1;
  const lensDot =
    mood === 'noticing' ? '#a78bfa' : mood === 'thinking' ? '#60a5fa' : '#2dd4bf';
  const showLensDot = mood === 'listening' || mood === 'noticing' || mood === 'thinking';

  return (
    <div
      className={`lana-sheep-wrap lana-sheep-wrap--${mood} ${className}`}
      style={{ transform: `scale(${scale})` }}
      aria-hidden
    >
      <svg
        className="lana-sheep-svg"
        viewBox="0 0 200 220"
        width="200"
        height="220"
        role="img"
        aria-label="Lana the sheep"
      >
        <defs>
          <filter id="lana-wool-soft" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="0.6" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <ellipse className="lana-sheep-shadow" cx="100" cy="208" rx="46" ry="9" fill="#c4b8a8" />

        <g className="lana-sheep-legs">
          <rect x="72" y="152" width="11" height="38" rx="5.5" fill="#e8b4a8" />
          <rect x="88" y="154" width="11" height="36" rx="5.5" fill="#e8b4a8" />
          <rect x="104" y="154" width="11" height="36" rx="5.5" fill="#e8b4a8" />
          <rect x="120" y="152" width="11" height="38" rx="5.5" fill="#e8b4a8" />
          <ellipse cx="77.5" cy="192" rx="8" ry="5" fill="#2b2b2b" />
          <ellipse cx="93.5" cy="192" rx="8" ry="5" fill="#2b2b2b" />
          <ellipse cx="109.5" cy="192" rx="8" ry="5" fill="#2b2b2b" />
          <ellipse cx="125.5" cy="192" rx="8" ry="5" fill="#2b2b2b" />
        </g>

        <g className="lana-sheep-body-group" filter="url(#lana-wool-soft)">
          {/* Fluffy wool — large cloud, face sits on lower edge */}
          <ellipse cx="100" cy="88" rx="62" ry="54" fill="#f7f3ec" />
          <ellipse cx="58" cy="92" rx="34" ry="30" fill="#faf7f2" />
          <ellipse cx="142" cy="92" rx="34" ry="30" fill="#faf7f2" />
          <ellipse cx="72" cy="72" rx="26" ry="24" fill="#fff" />
          <ellipse cx="128" cy="72" rx="26" ry="24" fill="#fff" />
          <ellipse cx="100" cy="62" rx="28" ry="26" fill="#fff" />
          <ellipse cx="82" cy="108" rx="22" ry="20" fill="#f3ede4" />
          <ellipse cx="118" cy="108" rx="22" ry="20" fill="#f3ede4" />
          <ellipse cx="100" cy="112" rx="38" ry="28" fill="#faf8f5" />

          {/* Ears */}
          <ellipse cx="68" cy="78" rx="11" ry="15" fill="#e8b4a8" transform="rotate(-22 68 78)" />
          <ellipse cx="132" cy="78" rx="11" ry="15" fill="#e8b4a8" transform="rotate(22 132 78)" />

          {/* Small muzzle — not a full body */}
          <ellipse cx="100" cy="122" rx="24" ry="17" fill="#e8b4a8" />

          {/* Wide sunglasses */}
          <rect x="68" y="112" width="64" height="22" rx="5" fill="#141414" />
          <rect x="72" y="115" width="26" height="16" rx="3" fill="#0a0a0a" opacity="0.9" />
          <rect x="102" y="115" width="26" height="16" rx="3" fill="#0a0a0a" opacity="0.9" />
          <rect x="98" y="112" width="4" height="22" rx="1" fill="#141414" />

          {showLensDot && (
            <circle className="lana-sheep-lens-dot" cx="79" cy="123" r="3.5" fill={lensDot} />
          )}
        </g>
      </svg>
    </div>
  );
}
