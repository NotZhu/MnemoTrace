(() => {
  'use strict';

  document.querySelectorAll('[data-particle-network]').forEach(canvas => {
    const context = canvas.getContext('2d'), host = canvas.parentElement;
    if (!context || !host) return;
    const graphEnabled = canvas.dataset.particleNetwork !== 'particles';
    const wordmark = canvas.dataset.particleNetwork === 'wordmark';
    const nodes = [], links = [], groups = [];
    let particleSeed = 20261012;
    const particleRandom = () => {
      particleSeed = (particleSeed * 1664525 + 1013904223) >>> 0;
      return particleSeed / 4294967296;
    };
    // The wordmark keeps a sparse particle field so its letters stay legible.
    const particles = Array.from({ length: wordmark ? 36 : 100 }, () => ({
      x: particleRandom(), y: particleRandom(), radius: 0.85 + particleRandom() * 1.3,
      speed: 2 + particleRandom() * 4, phase: particleRandom() * Math.PI * 2,
      opacity: 0.38 + particleRandom() * 0.5,
    }));
    // A pre-rendered halo keeps the star flares cheap; one sprite is reused for every particle.
    const glowSprite = (() => {
      const size = 64, sprite = document.createElement('canvas');
      sprite.width = sprite.height = size;
      const glow = sprite.getContext('2d');
      const gradient = glow.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
      gradient.addColorStop(0, 'rgba(216,232,255,0.95)');
      gradient.addColorStop(0.28, 'rgba(164,190,224,0.4)');
      gradient.addColorStop(0.62, 'rgba(156,177,207,0.1)');
      gradient.addColorStop(1, 'rgba(156,177,207,0)');
      glow.fillStyle = gradient;
      glow.fillRect(0, 0, size, size);
      return sprite;
    })();
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let width = 0, height = 0;
    let elapsed = 0, previous = 0, frameId = 0, visible = true, suspended = false;
    let lastFrameAt = performance.now();
    let hovered = -1, drag = null, wave = null;
    let groupOrigins = [], pulseCursor = 0, lastPulse = -2;
    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    // Stylized star patterns, not sky coordinates; explicit edges preserve recognizable silhouettes.
    const silhouettes = {
      dipper: {
        name: 'Ursa Major / Big Dipper',
        points: [[-0.95, -0.55], [-0.9, 0.25], [-0.23, 0.4], [-0.18, -0.32], [0.22, -0.13], [0.62, -0.27], [1, 0.02]],
        edges: [[0, 1], [1, 2], [2, 3], [3, 0], [3, 4], [4, 5], [5, 6]],
      },
      lyra: {
        name: 'Lyra',
        points: [[-0.85, -0.85], [-0.4, -0.2], [0.4, -0.4], [0.8, 0.55], [0, 0.75]],
        edges: [[0, 1], [1, 2], [2, 3], [3, 4], [4, 1]],
      },
      orion: {
        name: 'Orion',
        points: [[-0.65, -0.95], [0.65, -0.8], [-0.28, 0.05], [0, -0.05], [0.28, -0.15], [-0.7, 0.85], [0.75, 0.95]],
        edges: [[0, 1], [0, 2], [1, 4], [2, 3], [3, 4], [2, 5], [4, 6], [5, 6]],
      },
      taurus: {
        name: 'Taurus',
        points: [[-0.95, -0.9], [-0.65, -0.05], [0, 0.75], [0.4, 0.1], [0.8, -0.3], [1, -0.95], [-0.3, 0.35]],
        edges: [[0, 1], [1, 6], [6, 2], [2, 3], [3, 4], [4, 5]],
      },
      leo: {
        name: 'Leo',
        points: [[-0.45, -0.9], [-0.85, -0.85], [-1, -0.4], [-0.7, -0.1], [-0.55, 0.35], [-0.6, 0.85], [0.35, 0.15], [1, 0.7], [0.2, 0.85]],
        edges: [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [3, 6], [6, 7], [7, 8], [8, 6], [5, 8]],
      },
      cygnus: {
        name: 'Cygnus',
        points: [[0.15, -1], [0, -0.3], [-0.1, 0.3], [-0.25, 1], [-0.9, -0.05], [0.9, -0.55]],
        edges: [[0, 1], [1, 2], [2, 3], [1, 4], [1, 5]],
      },
    };
    const wordmarkShapes = wordmark ? [
      { name: 'M', points: [[0,9],[40,76],[80,9],[0,121],[80,121]], edges: [[3,0],[0,1],[1,2],[2,4]] },
      { name: 'n', points: [[0,39],[54,35],[0,110],[54,113]], edges: [[2,0],[0,1],[1,3]] },
      { name: 'e', points: [[0,52],[56,55],[0,85],[4,115],[54,118]], edges: [[0,1],[1,2],[2,0],[2,3],[3,4]] },
      { name: 'm', points: [[0,38],[44,56],[87,36],[0,113],[44,114],[87,114]], edges: [[3,0],[0,1],[1,4],[1,2],[2,5]] },
      { name: 'o', points: [[0,63],[56,55],[56,123],[-3,131]], edges: [[0,1],[1,2],[2,3],[3,0]] },
      { name: 'T', points: [[0,0],[40,0],[80,0],[40,114]], edges: [[0,1],[1,2],[1,3]] },
      { name: 'r', points: [[0,58],[40,55],[0,115]], edges: [[2,0],[0,1]] },
      { name: 'a', points: [[0,53],[53,42],[53,107],[-5,115],[53,120]], edges: [[0,1],[1,2],[2,3],[3,0],[2,4]] },
      { name: 'c', points: [[0,52],[56,57],[5,128],[56,120]], edges: [[1,0],[0,2],[2,3]] },
      { name: 'e', points: [[0,41],[56,44],[0,74],[4,104],[53,108]], edges: [[0,1],[1,2],[2,0],[2,3],[3,4]] },
    ].map(shape => {
      const centerX = (Math.min(...shape.points.map(point => point[0])) + Math.max(...shape.points.map(point => point[0]))) / 2;
      return { ...shape, points: shape.points.map(([x, y]) => [(x - centerX) / 64, (y - 65) / 65]) };
    }) : [];
    const arrangement = () => wordmark ? wordmarkShapes : width <= 680
      ? [silhouettes.dipper, silhouettes.lyra, silhouettes.orion, silhouettes.cygnus]
      : [silhouettes.dipper, silhouettes.taurus, silhouettes.lyra, silhouettes.orion, silhouettes.leo, silhouettes.cygnus];
    const activate = origin => {
      const depths = nodes.map(() => -1), queue = [origin];
      depths[origin] = 0;
      for (let cursor = 0; cursor < queue.length; cursor++) {
        const current = queue[cursor];
        nodes[current].neighbors.forEach(next => {
          if (depths[next] >= 0) return;
          depths[next] = depths[current] + 1; queue.push(next);
        });
      }
      wave = { origin, depths, started: elapsed, duration: Math.max(...depths) * 0.4 + 1.2 };
    };

    const home = node => {
      const group = groups[node.group], moving = !reduced.matches;
      const flex = wordmark ? 0.3 : 0.25;
      const driftX = moving ? Math.sin(elapsed * (wordmark ? 0.5 : 0.32) + group.phase) * group.drift : 0;
      const driftY = moving ? Math.cos(elapsed * (wordmark ? 0.42 : 0.27) + group.phase) * group.drift * 0.75 : 0;
      const bob = wordmark && moving ? Math.sin(elapsed * 0.8 + group.phase) * 3 : 0;
      return {
        x: clamp(group.u * width + node.localX * group.radius + driftX + (moving ? Math.sin(elapsed * 0.48 + node.phase) * 5 * flex : 0), 8, Math.max(8, width - 8)),
        y: clamp(group.v * height + node.localY * group.radius * 0.8 + driftY + bob + (moving ? Math.cos(elapsed * 0.4 + node.phase) * 4 * flex : 0), 8, Math.max(8, height - 8)),
      };
    };
    const build = () => {
      nodes.length = links.length = groups.length = 0;
      wave = null;
      let seed = 20261017;
      const random = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
      const compact = width <= 680;
      const shapes = arrangement();
      const rows = wordmark && compact ? 2 : 1, columns = wordmark ? shapes.length / rows : 0;
      const wordmarkSpread = 0.82;
      const anchors = wordmark ? shapes.map((_, index) => [
        0.5 + ((index % columns + 0.5) / columns - 0.5) * wordmarkSpread,
        (Math.floor(index / columns) + 0.5) / rows,
      ]) : compact ? [[0.18, 0.2], [0.81, 0.36], [0.17, 0.66], [0.8, 0.84]]
        : [[0.12, 0.26], [0.42, 0.14], [0.86, 0.24], [0.15, 0.75], [0.61, 0.84], [0.88, 0.7]];
      const addLink = (a, b) => {
        if (nodes[a].neighbors.has(b)) return;
        nodes[a].neighbors.add(b); nodes[b].neighbors.add(a);
        links.push({ a, b, length: Math.hypot(nodes[a].x - nodes[b].x, nodes[a].y - nodes[b].y) });
      };
      anchors.forEach(([u, v], groupIndex) => {
        const silhouette = shapes[groupIndex];
        const group = { u, v, radius: wordmark
          ? Math.min(height / rows * 0.48, width * wordmarkSpread / columns * 0.66) * 0.9
          : compact ? Math.min(64, width * 0.14) : Math.min(94, width * 0.076),
          silhouette, drift: wordmark ? 7 : compact ? 13 : 24, phase: random() * Math.PI * 2 };
        groups.push(group);
        const members = [], count = silhouette.points.length;
        for (let slot = 0; slot < count; slot++) {
          const [localX, localY] = silhouette.points[slot];
          const node = { group: groupIndex, localX, localY, vx: 0, vy: 0,
            bright: silhouette === silhouettes.lyra && slot === 0,
            phase: random() * Math.PI * 2, neighbors: new Set() };
          Object.assign(node, home(node));
          members.push(nodes.length); nodes.push(node);
        }
        silhouette.edges.forEach(([a, b]) => addLink(members[a], members[b]));
      });
      groupOrigins = groups.map((_, groupIndex) => nodes.findIndex(node => node.group === groupIndex));
    };
    const quietAt = (x, y) => {
      if (wordmark) return 1;
      const distance = Math.hypot((x / width - 0.5) / 0.3, (y / height - 0.44) / 0.42);
      return 0.28 + 0.72 * Math.min(1, distance);
    };
    const draw = () => {
      context.clearRect(0, 0, width, height);
      if (!width || !height) return;
      const focus = drag ? drag.node : hovered;
      // A slow scan gives the wordmark continuous motion instead of waiting for a click.
      const scanX = wordmark ? ((elapsed * 0.16) % 1.3 - 0.15) * width : -1;
      const scanAt = x => wordmark ? Math.max(0, 1 - Math.abs(x - scanX) / (width * 0.1)) : 0;
      // Login-style fine particles stay decorative, outside graph links and hit testing.
      context.fillStyle = '#a4bee0';
      context.shadowColor = 'rgba(156,188,233,0.55)';
      const particleCount = wordmark ? (width <= 680 ? 14 : particles.length) : width <= 680 ? 50 : particles.length;
      context.shadowBlur = 0;
      for (let index = 0; index < particleCount; index++) {
        const particle = particles[index];
        const x = particle.x * width + Math.sin(elapsed * 0.28 + particle.phase) * 7;
        const y = ((particle.y * height - elapsed * particle.speed) % (height + 40) + height + 40) % (height + 40) - 20;
        const fade = Math.min(1, Math.max(0, Math.min(y, height - y) / 45));
        const shimmer = 0.8 + 0.2 * Math.sin(elapsed * 0.6 + particle.phase);
        // Sharp, phase-warped pulses keep the flares irregular instead of a shared breathing rhythm.
        const pulse = 0.5 + 0.5 * Math.sin(elapsed * (0.2 + particle.phase * 0.07) + particle.phase * 2.1 + Math.sin(elapsed * 0.1 + particle.phase) * 0.9);
        const twinkle = reduced.matches ? 0 : Math.pow(pulse, 12);
        const presence = particle.opacity * fade * (wordmark ? 0.5 : 1) * quietAt(x, y);
        if (twinkle > 0.015) {
          const glowSize = particle.radius * (9 + twinkle * 12);
          context.globalAlpha = Math.min(0.6, twinkle * 0.5) * presence;
          context.drawImage(glowSprite, x - glowSize / 2, y - glowSize / 2, glowSize, glowSize);
        }
        context.globalAlpha = Math.min(1, (0.78 * shimmer + twinkle * 0.5) * presence);
        context.beginPath(); context.arc(x, y, particle.radius * (1 + twinkle * 0.35), 0, Math.PI * 2); context.fill();
      }
      context.globalAlpha = 1;
      context.shadowBlur = 0;
      if (!graphEnabled) return;
      links.forEach(link => {
        const a = nodes[link.a], b = nodes[link.b], active = link.a === focus || link.b === focus;
        const scan = (scanAt(a.x) + scanAt(b.x)) * 0.5;
        context.globalAlpha = Math.min(0.9, (active ? 0.68 : 0.24) + scan * 0.4)
          * quietAt((a.x + b.x) / 2, (a.y + b.y) / 2);
        context.strokeStyle = active ? '#c3daf8' : '#9cb1cf';
        context.lineWidth = active ? 1.15 : 0.75;
        context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y); context.stroke();
      });
      const age = wave ? elapsed - wave.started : 0;
      if (wave && !reduced.matches && age > wave.duration) wave = null;
      if (wave) {
        context.strokeStyle = '#d3e7ff'; context.lineWidth = 1.8;
        context.shadowColor = 'rgba(156,188,233,0.7)'; context.shadowBlur = 7;
        links.forEach(link => {
          const da = wave.depths[link.a], db = wave.depths[link.b];
          if (da < 0 || db < 0 || da === db) return;
          const from = nodes[da < db ? link.a : link.b], to = nodes[da < db ? link.b : link.a];
          const progress = reduced.matches ? 0.6 : (age - Math.min(da, db) * 0.4) / 0.4;
          if (progress < 0 || progress > 1.45) return;
          const tail = clamp(progress - 0.45, 0, 1), head = clamp(progress, 0, 1);
          if (head <= tail) return;
          context.globalAlpha = Math.pow(0.88, Math.min(da, db)) * (reduced.matches ? 0.65 : 0.95)
            * quietAt((from.x + to.x) / 2, (from.y + to.y) / 2);
          context.beginPath();
          context.moveTo(from.x + (to.x - from.x) * tail, from.y + (to.y - from.y) * tail);
          context.lineTo(from.x + (to.x - from.x) * head, from.y + (to.y - from.y) * head);
          context.stroke();
        });
      }
      nodes.forEach((node, index) => {
        const selected = index === focus, related = focus >= 0 && nodes[focus].neighbors.has(index);
        const depth = wave ? wave.depths[index] : -1;
        const sinceArrival = depth >= 0 ? age - depth * 0.4 : -1;
        const waveEnergy = depth < 0 ? 0 : reduced.matches ? 0.65
          : sinceArrival < 0 ? 0 : Math.max(0, 1 - sinceArrival / 1.1) * Math.pow(0.88, depth);
        const energy = Math.min(1, waveEnergy + scanAt(node.x) * 0.6);
        const radius = (wordmark && width <= 680 ? 1.6 : 2.6) + Math.min(node.neighbors.size, 4) * (wordmark && width <= 680 ? 0.25 : 0.45) + (node.bright ? 1 : 0) + (selected ? 1.5 : 0) + energy * 2;
        const shimmer = 0.88 + 0.12 * Math.sin(elapsed * 0.55 + node.phase);
        context.globalAlpha = Math.max(selected ? 1 : related || node.bright ? 0.9 : 0.74 * shimmer, energy) * quietAt(node.x, node.y);
        context.fillStyle = energy > 0 ? '#e2efff' : selected || related ? '#d3e4fa' : '#a4bee0';
        context.shadowColor = 'rgba(156,188,233,0.45)';
        context.shadowBlur = Math.max(selected ? 9 : related || node.bright ? 5 : 2, energy * 16);
        context.beginPath(); context.arc(node.x, node.y, radius, 0, Math.PI * 2); context.fill();
      });
      if (wave && !reduced.matches && age < 0.9) {
        const origin = nodes[wave.origin];
        context.globalAlpha = (1 - age / 0.9) * 0.7 * quietAt(origin.x, origin.y);
        context.strokeStyle = '#c3daf8'; context.lineWidth = 1; context.shadowBlur = 4;
        context.beginPath(); context.arc(origin.x, origin.y, 7 + age * 21, 0, Math.PI * 2); context.stroke();
      }
      context.globalAlpha = 1; context.shadowBlur = 0;
    };
    const step = delta => {
      const forces = nodes.map(node => {
        const target = home(node);
        return { x: (target.x - node.x) * 1.8, y: (target.y - node.y) * 1.8 };
      });
      links.forEach(link => {
        const a = nodes[link.a], b = nodes[link.b], dx = b.x - a.x, dy = b.y - a.y;
        const distance = Math.hypot(dx, dy) || 1, tension = (distance - link.length) * 2.8 / distance;
        forces[link.a].x += dx * tension; forces[link.a].y += dy * tension;
        forces[link.b].x -= dx * tension; forces[link.b].y -= dy * tension;
      });
      const separation = wordmark ? (width <= 680 ? 5 : 12) : 26;
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        if (nodes[i].group !== nodes[j].group) continue;
        const dx = nodes[j].x - nodes[i].x, dy = nodes[j].y - nodes[i].y, distance = Math.hypot(dx, dy);
        if (!distance || distance >= separation) continue;
        const force = (separation - distance) * 2 / distance;
        forces[i].x -= dx * force; forces[i].y -= dy * force;
        forces[j].x += dx * force; forces[j].y += dy * force;
      }
      nodes.forEach((node, index) => {
        if (drag && drag.node === index) { node.x = drag.x; node.y = drag.y; node.vx = node.vy = 0; return; }
        const damping = Math.exp(-3 * delta);
        node.vx = clamp((node.vx + forces[index].x * delta) * damping, -140, 140);
        node.vy = clamp((node.vy + forces[index].y * delta) * damping, -140, 140);
        node.x = clamp(node.x + node.vx * delta, 8, Math.max(8, width - 8));
        node.y = clamp(node.y + node.vy * delta, 8, Math.max(8, height - 8));
      });
    };
    const position = event => {
      const bounds = host.getBoundingClientRect();
      return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
    };
    const blocked = event => event.target instanceof Element
      && event.target.closest('a,button,input,select,textarea,[contenteditable],.stat');
    const nearest = point => {
      let found = -1, distance = 22;
      nodes.forEach((node, index) => {
        const next = Math.hypot(node.x - point.x, node.y - point.y);
        if (next < distance) { found = index; distance = next; }
      });
      return found;
    };
    const focus = node => { hovered = node; host.toggleAttribute('data-graph-hover', node >= 0); };
    const begin = (point, node, type, id) => {
      const current = nodes[node];
      drag = { node, type, id, startX: point.x, startY: point.y, moved: false,
        offsetX: current.x - point.x, offsetY: current.y - point.y, x: current.x, y: current.y };
      focus(node); host.setAttribute('data-graph-dragging', '');
    };
    const shift = point => {
      if (Math.hypot(point.x - drag.startX, point.y - drag.startY) > 6) drag.moved = true;
      drag.x = clamp(point.x + drag.offsetX, 8, Math.max(8, width - 8));
      drag.y = clamp(point.y + drag.offsetY, 8, Math.max(8, height - 8));
      const node = nodes[drag.node]; node.x = drag.x; node.y = drag.y; node.vx = node.vy = 0;
      draw();
    };
    const release = () => {
      const old = drag; drag = null; host.removeAttribute('data-graph-dragging');
      if (old && old.type === 'pointer' && host.hasPointerCapture(old.id)) host.releasePointerCapture(old.id);
      draw();
    };
    const move = event => {
      if (event.pointerType === 'touch' || event.isPrimary === false) return;
      const point = position(event);
      if (drag && drag.type === 'pointer' && drag.id === event.pointerId) shift(point);
      else { focus(blocked(event) ? -1 : nearest(point)); draw(); }
    };
    const down = event => {
      if (drag || event.pointerType === 'touch' || event.button !== 0 || event.isPrimary === false || blocked(event)) return;
      const point = position(event), node = nearest(point);
      if (node < 0) return;
      begin(point, node, 'pointer', event.pointerId); host.setPointerCapture(event.pointerId);
      event.preventDefault(); draw();
    };
    const up = event => {
      if (drag && drag.type === 'pointer' && drag.id === event.pointerId) {
        if (event.type === 'pointerup' && !drag.moved
          && Math.hypot(event.clientX - host.getBoundingClientRect().left - drag.startX,
            event.clientY - host.getBoundingClientRect().top - drag.startY) <= 6) activate(drag.node);
        release();
      }
    };
    const leave = () => { if (!drag) { focus(-1); draw(); } };
    const blur = () => { release(); focus(-1); draw(); };
    const touchStart = event => {
      if (drag || event.touches.length !== 1 || blocked(event)) return;
      const touch = event.changedTouches[0], point = position(touch), node = nearest(point);
      if (node < 0) return;
      begin(point, node, 'touch', touch.identifier); event.preventDefault(); draw();
    };
    const touchMove = event => {
      if (!drag || drag.type !== 'touch') return;
      const touch = Array.from(event.touches).find(item => item.identifier === drag.id);
      if (touch) { event.preventDefault(); shift(position(touch)); }
    };
    const touchEnd = event => {
      const touch = drag && drag.type === 'touch'
        ? Array.from(event.changedTouches).find(item => item.identifier === drag.id) : null;
      if (touch) {
        const point = position(touch);
        if (event.type === 'touchend' && !drag.moved
          && Math.hypot(point.x - drag.startX, point.y - drag.startY) <= 6) activate(drag.node);
        release(); focus(-1); draw();
      }
    };
    const handlers = graphEnabled ? { pointermove: move, pointerdown: down, pointerup: up, pointercancel: up,
      lostpointercapture: up, pointerleave: leave, touchstart: touchStart, touchmove: touchMove,
      touchend: touchEnd, touchcancel: touchEnd } : {};
    Object.entries(handlers).forEach(([name, handler]) => host.addEventListener(name, handler,
      name === 'touchstart' || name === 'touchmove' ? { passive: false } : undefined));
    window.addEventListener('blur', blur);
    const frame = now => {
      if (now - previous >= 1000 / 30) {
        const delta = Math.min(now - previous, 40) / 1000;
        previous = now; elapsed += delta;
        if (graphEnabled) step(delta);
        // The wordmark current pulses on its own without following a fixed timetable.
        if (wordmark && !reduced.matches && !suspended && !drag && !wave
          && groupOrigins.length && elapsed - lastPulse > 3.2) {
          lastPulse = elapsed;
          activate(groupOrigins[pulseCursor++ % groupOrigins.length]);
        }
        draw();
      }
      frameId = window.requestAnimationFrame(frame);
      lastFrameAt = performance.now();
    };
    // Embedded webviews can throttle requestAnimationFrame to about 1fps while
    // still showing the page; the timer keeps the graph moving in that case.
    const watchdog = window.setInterval(() => {
      if (reduced.matches || !visible || suspended) return;
      const now = performance.now();
      if (now - lastFrameAt < 120) return;
      if (frameId) { window.cancelAnimationFrame(frameId); frameId = 0; }
      frame(now);
    }, 1000 / 30);
    const playback = () => {
      window.cancelAnimationFrame(frameId); frameId = 0; previous = performance.now();
      if (document.hidden || !visible || suspended) { wave = null; blur(); }
      draw();
      if (width && height && visible && !suspended && !reduced.matches) frameId = window.requestAnimationFrame(frame);
    };
    const resize = () => {
      const bounds = host.getBoundingClientRect(); width = bounds.width; height = bounds.height;
      blur();
      if (!width || !height) { canvas.width = canvas.height = 0; playback(); return; }
      const count = graphEnabled ? arrangement().reduce((total, shape) => total + shape.points.length, 0) : 0;
      if (wordmark || nodes.length !== count) build();
      else {
        groups.forEach(group => { group.radius = width <= 680 ? Math.min(64, width * 0.14) : Math.min(94, width * 0.076); });
        nodes.forEach(node => { Object.assign(node, home(node)); node.vx = node.vy = 0; });
        links.forEach(link => { link.length = Math.hypot(nodes[link.a].x - nodes[link.b].x, nodes[link.a].y - nodes[link.b].y); });
      }
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0); playback();
    };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize);
    if (observer) observer.observe(host); else window.addEventListener('resize', resize);
    const intersection = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => {
      visible = entries[0].isIntersecting; playback();
    });
    if (intersection) intersection.observe(host);
    document.addEventListener('visibilitychange', playback); reduced.addEventListener('change', playback);
    window.addEventListener('pagehide', event => {
      suspended = true; playback();
      if (!event.persisted) {
        if (observer) observer.disconnect(); if (intersection) intersection.disconnect();
        window.removeEventListener('resize', resize); window.removeEventListener('blur', blur);
        document.removeEventListener('visibilitychange', playback); reduced.removeEventListener('change', playback);
        window.clearInterval(watchdog);
        Object.entries(handlers).forEach(([name, handler]) => host.removeEventListener(name, handler));
      }
    });
    window.addEventListener('pageshow', () => { suspended = false; resize(); });
    resize();
  });
})();
