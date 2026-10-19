import type { Config } from 'tailwindcss'

// 兼容现有颜色类名；实际色值与登录页共同来自 index.css 的蓝灰主题。
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        slate: {
          100: 'rgb(var(--memory-slate-100) / <alpha-value>)',
          200: 'rgb(var(--memory-slate-200) / <alpha-value>)',
          300: 'rgb(var(--memory-slate-300) / <alpha-value>)',
        },
        ink: {
          DEFAULT: 'rgb(var(--memory-background) / <alpha-value>)',
          900: 'rgb(var(--memory-background) / <alpha-value>)',
          800: 'rgb(var(--memory-surface) / <alpha-value>)',
          700: 'rgb(var(--memory-field) / <alpha-value>)',
          600: 'rgb(var(--memory-selected) / <alpha-value>)',
        },
        line: 'rgb(var(--memory-border) / <alpha-value>)',
        'field-line': 'rgb(var(--memory-field-border) / <alpha-value>)',
        'control-line': 'rgb(var(--memory-control-border) / <alpha-value>)',
        foreground: 'rgb(var(--memory-text) / <alpha-value>)',
        'icon-surface': 'rgb(var(--memory-icon-surface) / <alpha-value>)',
        selected: 'rgb(var(--memory-selected) / <alpha-value>)',
        action: 'rgb(var(--memory-button) / <alpha-value>)',
        'action-hover': 'rgb(var(--memory-button-hover) / <alpha-value>)',
        secondary: 'rgb(var(--memory-secondary) / <alpha-value>)',
        'secondary-hover': 'rgb(var(--memory-secondary-hover) / <alpha-value>)',
        brand: {
          cyan: 'rgb(var(--memory-accent) / <alpha-value>)',
          violet: 'rgb(var(--memory-violet) / <alpha-value>)',
          mint: 'rgb(var(--memory-mint) / <alpha-value>)',
          rose: 'rgb(var(--memory-rose) / <alpha-value>)',
          amber: 'rgb(var(--memory-amber) / <alpha-value>)',
        },
        ghost: 'rgb(var(--memory-muted) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['Inter', 'PingFang SC', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      boxShadow: {
        card: 'var(--memory-card-shadow)',
      },
      keyframes: {
        'fade-up': {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'fade-up': 'fade-up 0.35s ease both',
      },
    },
  },
  plugins: [],
} satisfies Config
