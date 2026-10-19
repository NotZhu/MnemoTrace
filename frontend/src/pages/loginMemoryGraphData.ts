// 脑形、独立点线和电流共用 1254 × 1254 坐标系。
// 基础绘制与交互读取同一份坐标，避免静态圆点和电流落点再次分离。
import { edgeColors, hasVisibleStroke } from './loginMemoryGraphAppearance'

export type BrainNode = { x: number; y: number; radius: number; neighbors: number[] }
export type BrainEdge = { from: number; to: number; path: string; reverse: string }
type Point = [number, number, number?]
export const nodes: BrainNode[] = []
const candidateEdges: BrainEdge[] = []
const lookup = new Map<string, number>()
const edgeKeys = new Set<string>()
function node(point: Point) {
  const key = point[0] + ',' + point[1]
  const existing = lookup.get(key)
  if (existing !== undefined) return existing
  const id = nodes.length
  nodes.push({ x: point[0], y: point[1], radius: point[2] ?? 6, neighbors: [] })
  lookup.set(key, id)
  return id
}
function chain(points: Point[], bend = 0.06) {
  const ids = points.map(node)
  for (let i = 1; i < ids.length; i++) {
    const from = ids[i - 1], to = ids[i]
    const key = [from, to].sort((a, b) => a - b).join('-')
    if (edgeKeys.has(key)) continue
    edgeKeys.add(key)
    const a = nodes[from], b = nodes[to], dx = b.x - a.x, dy = b.y - a.y
    const k = bend * (i % 2 ? 1 : -1)
    const c1 = [a.x + dx / 3 - dy * k, a.y + dy / 3 + dx * k]
    const c2 = [a.x + 2 * dx / 3 - dy * k, a.y + 2 * dy / 3 + dx * k]
    candidateEdges.push({ from, to, path: `M ${a.x} ${a.y} C ${c1} ${c2} ${b.x} ${b.y}`, reverse: `M ${b.x} ${b.y} C ${c2} ${c1} ${a.x} ${a.y}` })
  }
}
  const A: Point=[404,226,13],B: Point=[700,253,14],C: Point=[309,300,9],D: Point=[508,316,9],E: Point=[660,360,11],F: Point=[927,377,12],G: Point=[840,503,16],H: Point=[574,494,13],I: Point=[379,587,9],J: Point=[690,662,13],K: Point=[1021,634,15],L: Point=[819,790,14],M: Point=[386,799,9],N: Point=[840,919,12];
  chain([A,[446,161,7],[495,137,6],[546,170,8],[583,215,6],B],0.15);
  chain([A,[503,187,7],[546,170,8]],0.12);
  chain([A,[386,171,5]]);chain([A,[307,213,6],[328,254,6],C]);
  chain([A,[365,288,5],C],0.1);
  chain([A,[441,290,6],D],0.1);
  chain([C,[271,264,6],[262,251,5],[226,281,6],[216,328,8],[163,324,5]]);
  chain([C,[263,371,9],[213,434,8],[177,394,7],[125,383,6]],0.1);
  chain([[213,434,8],[142,463,5],[85,463,6]]);
  chain([[142,463,5],[110,515,8],[67,536,5]]);
  chain([[110,515,8],[164,552,6],[173,606,9],[105,632,6]]);
  chain([[173,606,9],[184,657,5],[171,682,5]]);
  chain([[173,606,9],[267,679,9],[310,618,7],I],0.09);
  chain([[263,371,9],[276,478,6],[340,525,8],I],0.16);
  chain([C,[368,345,6],[420,366,6],[511,420,6],[540,394,6],D],0.09);
  chain([[441,290,6],[420,366,6]]);
  chain([D,[480,252,5]]);chain([D,[562,287,5],[544,257,6]]);
  chain([[562,287,5],[603,298,6],E],0.1);
  chain([B,[647,191,9],[593,172,7],[571,126,5]]);
  chain([B,[719,179,8],[729,132,5]]);
  chain([[719,179,8],[776,167,5]]);
  chain([B,[768,204,5],[812,205,6],[826,171,5]],0.08);
  chain([[812,205,6],[858,231,5],[897,286,10],[931,228,6]],0.08);
  chain([[897,286,10],[943,264,6],[970,265,2]]);
  chain([[897,286,10],[850,286,6]]);
  chain([[897,286,10],[970,291,5],[1006,293,6],[969,330,4],F]);
  chain([[1006,293,6],[1012,334,6],[1008,386,9],F],0.14);
  chain([[1008,386,9],[1074,379,5]]);chain([F,[974,432,6]]);chain([F,[925,444,6]],0.12);
  chain([G,[950,503,7]],0.12);
  chain([[1008,386,9],[1034,449,8],[1094,437,6]]);
  chain([[1034,449,8],[1071,480,4],[1098,519,9],[1147,505,5]]);
  chain([[1098,519,9],[1174,584,5],[1172,684,6]],0.1);
  chain([[1098,519,9],[1070,555,6],[1109,575,5]]);
  chain([[1070,555,6],K]);
  chain([[950,503,7],K,[1152,634,5],[1172,684,6]],0.12);
  chain([[1172,684,6],[1106,713,9],[1155,773,5]]);
  chain([[1106,713,9],[1057,741,6]]);chain([[1106,713,9],[1074,784,6],[1042,824,8],[1096,832,5]]);
  chain([K,[1035,697,5]]);chain([K,[1021,718,5]]);
  chain([K,[896,663,8],[927,615,5],[940,575,5],G],0.14);
  chain([G,[849,572,6]]);chain([G,[775,572,8],[811,623,6],[870,629,6],[896,663,8]]);
  chain([G,[880,428,5],[863,386,6]]);chain([[880,428,5],F]);
  chain([B,[804,253,6]]);chain([B,[782,305,5],[814,359,8],[860,331,3],[897,286,10]],0.12);
  chain([[814,359,8],[761,347,7]]);chain([[814,359,8],[802,404,8],[708,410,7],[719,369,5]]);
  chain([[708,410,7],E,[626,445,7],H],0.1);
  chain([E,[580,348,6]]);chain([E,[634,283,7],B],0.09);
  chain([[511,420,6],[535,458,4],H,[508,532,7]]);
  chain([H,[573,564,5],[504,617,8],I],0.12);
  chain([H,[638,562,3],[683,582,7],[674,534,6],[702,475,7],[745,455,8],G],0.1);
  chain([[802,404,8],[745,455,8]]);
  chain([[683,582,7],J,[614,631,6],[573,564,5]],0.12);
  chain([J,[758,733,7]]);chain([J,[680,726,4]]);
  chain([J,[709,767,6]]);chain([J,[836,715,8],L],0.11);
  chain([[896,663,8],[836,715,8]]);
  chain([L,[577,743,6]],0.05);
  chain([L,[910,795,5],[911,810,5]]);chain([L,[926,722,6]]);
  chain([L,[756,822,6],[680,869,8],[657,925,6]]);
  chain([L,[964,775,8],[1011,787,5],[1042,824,8]]);
  chain([[504,617,8],[527,671,5],[464,709,6],[432,756,6],M],0.1);
  chain([[267,679,9],[362,736,6],M,[338,816,5]]);
  chain([M,[431,822,6],[465,858,7],[550,876,6]]);
  chain([[465,858,7],[508,837,6],[556,796,6],[641,805,9],[680,869,8]],0.08);
  chain([[641,805,9],[709,767,6]]);
  chain([L,N,[782,896,6]]);chain([N,[772,942,5]]);chain([N,[821,974,5]]);
  chain([N,[881,876,6],[939,905,8],[970,878,5],[1012,894,5]]);
  chain([N,[914,939,7],[933,984,6]]);chain([[914,939,7],[939,905,8],[987,934,5]]);
  chain([N,[875,978,6],[881,1015,5]]);
  chain([[680,869,8],[733,1003,7],[765,1063,7],[795,1119,7]],0.06);

// 插画取样曾把部分连线取成背景色：它们不能成为电流的隐形捷径。
// 绘制、常驻电流和点击传播都使用最终的可见连接，再据此建立邻接关系。
export const edges = candidateEdges.filter(edge => {
  const from = nodes[edge.from], to = nodes[edge.to]
  const key = [`${from.x},${from.y}`, `${to.x},${to.y}`].sort().join('|')
  return hasVisibleStroke(edgeColors[key])
})
for (const edge of edges) {
  nodes[edge.from].neighbors.push(edge.to)
  nodes[edge.to].neighbors.push(edge.from)
}

export function distances(origin: number) {
  const depths = nodes.map(() => -1)
  const queue = [origin]
  depths[origin] = 0
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const current = queue[cursor]
    for (const neighbor of nodes[current].neighbors) {
      if (depths[neighbor] !== -1) continue
      depths[neighbor] = depths[current] + 1
      queue.push(neighbor)
    }
  }
  return depths
}

export const AMBIENT_INTERVAL_MS = 3250
export const AMBIENT_NODE_FADE_MS = 1000
const AMBIENT_HOPS = 3
export const ambientOrigins = [
  [700, 253], [1021, 634], [386, 799], [840, 919], [404, 226], [840, 503],
].map(([x, y]) => nodes.findIndex(node => node.x === x && node.y === y && node.neighbors.length > 0))
  .filter(index => index >= 0)

type SignalPath = { edge: BrainEdge; from: number; to: number; depth: number; delayMs: number; durationMs: number }
export type AmbientSignal = {
  origin: number
  paths: SignalPath[]
  litNodes: { index: number; depth: number; delayMs: number }[]
  durationMs: number
}

export function createAmbientSignal(origin: number): AmbientSignal {
  const depths = distances(origin)
  const arrivals = nodes.map(() => Infinity)
  const incoming = new Map<number, SignalPath>()
  arrivals[origin] = 0
  for (let depth = 0; depth < AMBIENT_HOPS; depth++) {
    for (const edge of edges) {
      const from = depths[edge.from] < depths[edge.to] ? edge.from : edge.to
      const to = from === edge.from ? edge.to : edge.from
      if (depths[from] !== depth || depths[to] !== depth + 1 || !Number.isFinite(arrivals[from])) continue
      const a = nodes[from], b = nodes[to]
      const durationMs = Math.round(Math.max(135, Math.min(440, Math.hypot(b.x - a.x, b.y - a.y) / 0.43)) * 1.55)
      const arrival = arrivals[from] + durationMs
      if (arrival >= arrivals[to]) continue
      arrivals[to] = arrival
      incoming.set(to, { edge, from, to, depth, delayMs: arrivals[from], durationMs })
    }
  }
  // 每个亮起节点都对应一条实际到达它的入边，避免环路中的提前亮点。
  const litNodes = arrivals.flatMap((delayMs, index) => Number.isFinite(delayMs)
    ? [{ index, depth: depths[index], delayMs }] : [])
  return {
    origin,
    paths: [...incoming.values()],
    litNodes,
    durationMs: Math.max(...litNodes.map(node => node.delayMs)) + AMBIENT_NODE_FADE_MS,
  }
}
