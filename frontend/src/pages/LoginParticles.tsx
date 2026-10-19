import { useEffect, useRef } from 'react'

// 集中调整背景微粒，增强可见度时仍保持舒缓的移动速度。
const PARTICLES = { count: 100, strength: 78, speed: 70, fps: 30 }

export function LoginParticles() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    const container = canvas?.parentElement
    const context = canvas?.getContext('2d')
    if (!canvas || !container || !context) return

    let seed = 20261012
    const random = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0
      return seed / 4294967296
    }
    const particles = Array.from({ length: PARTICLES.count }, () => ({
      x: random(), y: random(), radius: 0.85 + random() * 1.3,
      speed: 2 + random() * 4, phase: random() * Math.PI * 2, opacity: 0.38 + random() * 0.5,
    }))
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
    let width = 0, height = 0, elapsed = 0, frameId = 0, previous = 0
    let color = '#a4bee0', glow = 'rgb(156 188 233 / 55%)'
    let glowSprite: HTMLCanvasElement | null = null
    let glowScale = 1

    // 预渲染光晕贴图，按主题色染色，避免每帧设置 shadowBlur。
    const buildGlowSprite = () => {
      const size = 64
      const sprite = document.createElement('canvas')
      sprite.width = sprite.height = size
      const spriteContext = sprite.getContext('2d')
      if (!spriteContext) return null
      const gradient = spriteContext.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
      gradient.addColorStop(0, 'rgba(255,255,255,0.95)')
      gradient.addColorStop(0.28, 'rgba(255,255,255,0.4)')
      gradient.addColorStop(0.62, 'rgba(255,255,255,0.1)')
      gradient.addColorStop(1, 'rgba(255,255,255,0)')
      spriteContext.fillStyle = gradient
      spriteContext.fillRect(0, 0, size, size)
      spriteContext.globalCompositeOperation = 'source-in'
      spriteContext.fillStyle = color
      spriteContext.fillRect(0, 0, size, size)
      return sprite
    }

    // 用主题里的 glow alpha 控制光晕强度，浅色主题默认更克制。
    const readGlowScale = (value: string) => {
      const numbers = value.match(/[\d.]+/g)
      if (!numbers) return 1
      const last = parseFloat(numbers[numbers.length - 1])
      const alpha = value.includes('%') ? last / 100 : last
      return Math.max(0, Math.min(1, alpha / 0.55))
    }

    const draw = () => {
      context.clearRect(0, 0, width, height)
      if (!width || !height) return
      const time = elapsed * PARTICLES.speed / 70
      context.fillStyle = color
      context.shadowBlur = 0
      for (const particle of particles) {
        const x = particle.x * width + Math.sin(time * 0.28 + particle.phase) * 7
        const y = ((particle.y * height - time * particle.speed) % (height + 40) + height + 40) % (height + 40) - 20
        const fade = Math.min(1, Math.max(0, Math.min(y, height - y) / 45))
        const shimmer = 0.8 + 0.2 * Math.sin(time * 0.6 + particle.phase)
        // 与落地页一致的随机脉冲：每颗粒子独立慢闪，亮起时间很短。
        const pulse = 0.5 + 0.5 * Math.sin(time * (0.2 + particle.phase * 0.07) + particle.phase * 2.1 + Math.sin(time * 0.1 + particle.phase) * 0.9)
        const twinkle = reduced.matches ? 0 : Math.pow(pulse, 12)
        const presence = PARTICLES.strength / 100 * particle.opacity * fade
        if (twinkle > 0.015 && glowSprite) {
          const glowSize = particle.radius * (9 + twinkle * 12)
          context.globalAlpha = Math.min(0.6, twinkle * 0.5) * presence * glowScale
          context.drawImage(glowSprite, x - glowSize / 2, y - glowSize / 2, glowSize, glowSize)
        }
        context.globalAlpha = Math.min(1, (0.78 * shimmer + twinkle * 0.5) * presence)
        context.beginPath()
        context.arc(x, y, particle.radius * (1 + twinkle * 0.35), 0, Math.PI * 2)
        context.fill()
      }
      context.globalAlpha = 1
    }
    const resize = () => {
      const bounds = container.getBoundingClientRect()
      width = bounds.width; height = bounds.height
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio)
      context.setTransform(ratio, 0, 0, ratio, 0, 0)
      const style = getComputedStyle(canvas)
      color = style.getPropertyValue('--login-particle-color').trim() || color
      glow = style.getPropertyValue('--login-particle-glow').trim() || glow
      glowScale = readGlowScale(glow)
      glowSprite = buildGlowSprite()
      draw()
    }
    const frame = (now: number) => {
      if (now - previous >= 1000 / PARTICLES.fps) {
        // 后台停帧后从当前时刻续播，不补播积压的移动距离。
        elapsed += Math.min(now - previous, 80) / 1000
        previous = now
        draw()
      }
      frameId = window.requestAnimationFrame(frame)
    }
    const updatePlayback = () => {
      window.cancelAnimationFrame(frameId)
      previous = performance.now()
      draw()
      if (!document.hidden && !reduced.matches) frameId = window.requestAnimationFrame(frame)
    }
    const observer = new ResizeObserver(resize)
    observer.observe(container)
    const themeObserver = new MutationObserver(resize)
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    resize()
    updatePlayback()
    document.addEventListener('visibilitychange', updatePlayback)
    reduced.addEventListener('change', updatePlayback)
    return () => {
      window.cancelAnimationFrame(frameId)
      observer.disconnect()
      themeObserver.disconnect()
      document.removeEventListener('visibilitychange', updatePlayback)
      reduced.removeEventListener('change', updatePlayback)
    }
  }, [])

  return <canvas ref={canvasRef} className="login-particles" aria-hidden="true" />
}
