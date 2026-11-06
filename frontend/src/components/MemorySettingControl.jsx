import { SEMANTIC_STATUS_STYLES } from '../lib/semanticStatusStyles.js'

function MemorySettingControl({
  checked,
  disabled = false,
  isDark,
  onChange,
  variant,
}) {
  if (variant === 'checkbox') {
    return (
      <span className="relative inline-flex h-6 w-6 shrink-0 cursor-pointer items-center justify-center">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          aria-label="Enable long-term memory"
          aria-busy={disabled}
          className="peer absolute inset-0 z-10 h-6 w-6 cursor-pointer opacity-0 disabled:cursor-wait"
        />
        <span
          aria-hidden="true"
          className={`flex h-6 w-6 items-center justify-center rounded-md border transition-all duration-200 peer-focus-visible:ring-2 peer-focus-visible:ring-violet-400 peer-focus-visible:ring-offset-2 peer-disabled:opacity-60 ${
            isDark ? 'peer-focus-visible:ring-offset-slate-900' : 'peer-focus-visible:ring-offset-white'
          } ${
            checked
              ? SEMANTIC_STATUS_STYLES.positive.solid
              : isDark
                ? 'border-slate-600 bg-slate-800 text-transparent hover:border-violet-400'
                : 'border-slate-300 bg-white text-transparent hover:border-violet-400'
          }`}
        >
          <svg
            viewBox="0 0 16 16"
            fill="none"
            className={`h-4 w-4 transition duration-200 ${
              checked ? 'scale-100 opacity-100' : 'scale-75 opacity-0'
            }`}
          >
            <path
              d="m3.5 8.25 3 3 6-6.5"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="2"
            />
          </svg>
        </span>
      </span>
    )
  }

  const status = checked ? 'ON' : 'OFF'

  return (
    <div className="flex shrink-0 items-center gap-3">
      <span
        aria-live="polite"
        className={`min-w-8 text-right text-xs font-bold tracking-wide ${
          checked
            ? SEMANTIC_STATUS_STYLES.positive.text
            : isDark ? 'text-slate-400' : 'text-slate-500'
        }`}
      >
        {disabled && !checked && status === 'OFF' ? 'OFF' : status}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label="Automatic Memory"
        aria-busy={disabled}
        title={disabled ? 'Memory preference is being updated' : `Automatic Memory ${status}`}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`group relative inline-flex h-7 w-[3.25rem] shrink-0 cursor-pointer items-center rounded-full p-1 transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-400 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-60 ${
          isDark ? 'focus-visible:ring-offset-slate-900' : 'focus-visible:ring-offset-white'
        } ${
          checked
            ? SEMANTIC_STATUS_STYLES.positive.toggle
            : isDark
              ? 'bg-slate-700 hover:bg-slate-600'
              : 'bg-slate-300 hover:bg-slate-400'
        }`}
      >
        <span
          aria-hidden="true"
          className={`h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            disabled ? 'animate-pulse' : ''
          } ${
            checked ? 'translate-x-5' : 'translate-x-0'
          }`}
        />
      </button>
    </div>
  )
}

export default MemorySettingControl
