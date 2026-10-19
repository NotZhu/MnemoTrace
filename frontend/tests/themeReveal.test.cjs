const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/lib/themeReveal.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
  external: ['react-dom', '../store/theme'],
}).outputFiles[0].text

function deferred() { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b}); return {promise,resolve,reject} }
function setup({supported=true, reduced=false, failStart=false}={}) {
  const calls=[], animations=[], transitions=[]
  const root={dataset:{},animate(frames,options){const animation={cancelled:false,cancel(){this.cancelled=true}};animations.push({frames,options,animation});return animation}}
  const document={documentElement:root}
  if(supported) document.startViewTransition=callback=>{
    if(failStart) throw new Error('capture unavailable')
    const ready=deferred(),finished=deferred()
    const transition={ready:ready.promise,finished:finished.promise,updateCallbackDone:Promise.resolve(),skipped:false,skipTransition(){this.skipped=true;finished.resolve()}}
    callback();transitions.push({transition,ready,finished});return transition
  }
  const moduleData={exports:{}}
  vm.runInNewContext(source,{module:moduleData,exports:moduleData.exports,document,window:{innerWidth:800,innerHeight:600,matchMedia:()=>({matches:reduced})},require:name=>{
    if(name==='react-dom') return {flushSync:fn=>fn()}
    if(name==='../store/theme') return {useTheme:{getState:()=>({toggle:animate=>calls.push(animate)})}}
    throw new Error(name)
  }})
  return {root,calls,animations,transitions,reveal:()=>moduleData.exports.revealThemeFrom({getBoundingClientRect:()=>({left:708,top:16,width:72,height:36})})}
}
async function settle() { await Promise.resolve();await Promise.resolve() }

test('圆形从按钮中心向外覆盖视口，主题只更新一次并关闭内部颜色渐变',async()=>{
  const s=setup();s.reveal();assert.deepEqual(s.calls,[false]);assert.equal(s.root.dataset.themeReveal,'true')
  s.transitions[0].ready.resolve();await settle()
  const {frames,options}=s.animations[0]
  assert.equal(frames.clipPath[0],'circle(0px at 744px 34px)')
  const radius=Number(frames.clipPath[1].match(/circle\((\d+)px/)[1])
  assert(radius>=Math.hypot(744,566));assert.equal(options.pseudoElement,'::view-transition-new(root)')
  s.transitions[0].finished.resolve();await settle();assert.equal(s.root.dataset.themeReveal,undefined)
})
test('不支持、减少动态效果或快照启动失败时正常切换，不残留样式',()=>{
  for(const options of [{supported:false},{reduced:true},{failStart:true}]) {
    const s=setup(options);s.reveal();assert.deepEqual(s.calls,[undefined]);assert.equal(s.root.dataset.themeReveal,undefined)
  }
})
test('连续切换取消旧动画，旧任务结束不会清除新任务样式',async()=>{
  const s=setup();s.reveal();s.transitions[0].ready.resolve();await settle()
  s.reveal();await settle();assert(s.transitions[0].transition.skipped);assert(s.animations[0].animation.cancelled)
  assert.equal(s.root.dataset.themeReveal,'true');assert.equal(s.calls.length,2)
  s.transitions[1].ready.resolve();await settle();s.transitions[1].finished.resolve();await settle()
  assert.equal(s.root.dataset.themeReveal,undefined)
})
test('快照准备失败仍保留主题更新，并清理动画标记',async()=>{
  const s=setup();s.reveal();s.transitions[0].ready.reject(new Error('not visible'));await settle();await settle()
  assert.deepEqual(s.calls,[false]);assert.equal(s.animations.length,0);assert.equal(s.root.dataset.themeReveal,undefined)
})
