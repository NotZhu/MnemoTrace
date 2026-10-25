const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')

const modelSource = esbuild.buildSync({
  stdin: {contents: "export * from './src/vendor/quartz/model'; export * from './src/vendor/quartz/view'; export * from './src/vendor/quartz/motion'; export {zoomIdentity} from 'd3'", resolveDir: path.resolve(__dirname, '..')},
  bundle:true, platform:'node', format:'cjs', write:false,
}).outputFiles[0].text
const modelModule = {exports:{}}
vm.runInNewContext(modelSource, {module:modelModule,exports:modelModule.exports,setTimeout,clearTimeout,setInterval,clearInterval,performance})
const graphSource = esbuild.buildSync({
  entryPoints:[path.resolve(__dirname,'../src/vendor/quartz/graph.ts')],
  bundle:true,platform:'node',format:'cjs',write:false,
  external:['d3','pixi.js','@tweenjs/tween.js','./model'],
}).outputFiles[0].text

const baseGraph={nodes:Array.from({length:15},(_,i)=>({id:String(i),name:`合成实体 ${i}`,type:'entity'})),
  edges:[...Array.from({length:8},(_,i)=>({source:'0',target:String(i+1)})),...[[9,10],[11,12],[13,14]].map(([a,b])=>({source:String(a),target:String(b)}))]
    .map(edge=>({...edge,predicate:'related_to',live:true}))}

async function scene({reducedMotion=false,data=baseGraph,viewCache,liveOnly=false,settings={},selectedId=null}={}) {
  let simulation, stage, zoomHandler, dragSubject
  let restarts=0
  let renderFrame
  const dragHandlers={}
  const transforms = [], fitAlphas = []
  const vector = () => ({x:0,y:0,set(x,y=x) {this.x=x;this.y=y}})
  class Container {
    constructor() {this.children=[];this.scale=vector();this.position=vector()}
    addChild(...children) {this.children.push(...children)}
    removeChildren() {return this.children.splice(0)}
    destroy() {}
  }
  class Graphics extends Container {
    on(name,handler) {this.handlers??={};this.handlers[name]=handler;return this}
    clear() {return this}
    circle() {return this}
    fill() {return this}
    moveTo() {return this}
    lineTo() {return this}
    stroke(style) {this.lastStroke=style;return this}
  }
  class Text extends Container {
    constructor({text,style}) {super();this.text=text;this.style=style}
    getLocalBounds() {return {width:this.text.length*6}}
  }
  class Application {
    constructor() {
      this.stage=stage=new Container()
      this.canvas={setAttribute(){},addEventListener(){},removeEventListener(){},style:{}}
      this.renderer={render(){},resize(){}}
    }
    async init() {}
    destroy() {}
  }
  const surface={call(fn,...args) {fn(surface,...args);return surface},on() {return surface}}
  function drag() {
    const d=()=>{};d.container=()=>d;d.on=(name,handler)=>{dragHandlers[name]=handler;return d};d.subject=callback=>{dragSubject=callback;return d};return d
  }
  function zoom() {
    const z=()=>{}
    z.extent=z.scaleExtent=()=>z
    z.on=(_,handler)=>{zoomHandler=handler;return z}
    z.transform=(_,transform)=>{transforms.push(transform.k);fitAlphas.push(simulation.alpha());zoomHandler({transform})}
    z.scaleBy=(_,factor)=>z.transform(surface,modelModule.exports.zoomIdentity.translate(stage.position.x,stage.position.y).scale(stage.scale.x*factor))
    z.translateBy=(_,x,y)=>z.transform(surface,modelModule.exports.zoomIdentity.translate(stage.position.x+x*stage.scale.x,stage.position.y+y*stage.scale.x).scale(stage.scale.x))
    return z
  }
  const moduleData={exports:{}}
  vm.runInNewContext(graphSource, {
    module:moduleData,exports:moduleData.exports,DOMException,performance,
    window:{devicePixelRatio:1,matchMedia:()=>({matches:reducedMotion,addEventListener(){},removeEventListener(){}})},
    document:{documentElement:{}},getComputedStyle:()=>({fontFamily:'sans-serif',getPropertyValue:()=>''}),
    MutationObserver:class {observe(){} disconnect(){}},
    requestAnimationFrame:callback=>{renderFrame=callback;return 1},cancelAnimationFrame(){renderFrame=undefined},
    require(name) {
      if(name==='./model') return {...modelModule.exports,createQuartzSimulation(...args) {
        simulation=modelModule.exports.createQuartzSimulation(...args)
        simulation.restart=()=>{restarts++;return simulation}
        return simulation
      }}
      if(name==='d3') return {select:()=>surface,drag,dragEnable(){},zoom,zoomIdentity:modelModule.exports.zoomIdentity}
      if(name==='pixi.js') return {Application,Container,Graphics,Text,Circle:class {}}
      return require(name)
    },
  })
  const controller=await moduleData.exports.createQuartzGraph({clientWidth:900,clientHeight:500,appendChild(){}},data,{
    settings:{nodeScale:1.4,lineWidth:0.8,repelForce:1,centerForce:0.2,linkDistance:110,linkForce:0.6,showRelations:false,opacityScale:3,...settings},
    selectedId,onSelect(){},signal:new AbortController().signal,viewCache,liveOnly,
  })
  return {controller,simulation,stage,transforms,fitAlphas,restarts:()=>restarts,
    wheelZoom(factor) {zoomHandler({transform:modelModule.exports.zoomIdentity.translate(stage.position.x,stage.position.y).scale(stage.scale.x*factor),sourceEvent:{type:'wheel'}})},
    dragStart(node) {dragHandlers.start({subject:node})},
    dragEnd(node) {dragHandlers.end({subject:node})},
    hitNode(node) {return dragSubject({x:(node.x+450)*stage.scale.x+stage.position.x,y:(node.y+250)*stage.scale.x+stage.position.y})?.id},
    draw(elapsed=0) {const callback=renderFrame;renderFrame=undefined;callback?.(performance.now()+elapsed)},finish() {
    if (simulation.alpha() >= simulation.alphaMin()) simulation.tick(300)
    simulation.on('end.render')?.()
    const callback=renderFrame;renderFrame=undefined;callback?.(performance.now()+10_000)
  }}
}

test('筛选替换数据保留相机和已有坐标，返回节点恢复位置，空结果不重启布局',async()=>{
  for (const reducedMotion of [true,false]) {
    const cache=modelModule.exports.graphViewCache(`synthetic-filter-${reducedMotion}`)
    const s=await scene({viewCache:cache,reducedMotion})
    try {
      s.finish()
      s.controller.zoomBy(1.2);s.controller.panBy(30,20)
      const positions=new Map(s.simulation.nodes().map(n=>[n.id,[n.x,n.y]]))
      const view=JSON.stringify([s.stage.scale.x,s.stage.position.x,s.stage.position.y])
      const subset={nodes:baseGraph.nodes.slice(0,2),edges:baseGraph.edges.slice(0,1)}
      const extra={nodes:[...subset.nodes,{id:'new',name:'合成新节点',type:'entity'}],edges:[...subset.edges,{source:'0',target:'new',predicate:'uses',live:true}]}
      for(const data of [subset,extra,{nodes:[],edges:[]},baseGraph]) {
        const restarts=s.restarts()
        s.controller.setData(data,{preserveView:true})
        s.draw(1000)
        assert.equal(JSON.stringify([s.stage.scale.x,s.stage.position.x,s.stage.position.y]),view)
        assert.equal(s.restarts(),restarts)
        for(const n of s.simulation.nodes()) {
          if(positions.has(n.id)) assert.equal(JSON.stringify([n.x,n.y]),JSON.stringify(positions.get(n.id)))
          assert.ok(Number.isFinite(n.x)&&Number.isFinite(n.y))
          assert.equal(n.fx,undefined);assert.equal(n.fy,undefined)
        }
      }
    } finally {s.controller.destroy()}
  }
})

test('入场中筛选保存可见坐标，旧动画不再覆盖返回节点的位置',async()=>{
  const s=await scene({viewCache:modelModule.exports.graphViewCache('synthetic-mid-entrance')})
  try {
    const positions=JSON.stringify(s.simulation.nodes().map(n=>[n.id,n.x,n.y]))
    s.controller.setData({nodes:baseGraph.nodes.slice(0,2),edges:baseGraph.edges.slice(0,1)},{preserveView:true})
    s.controller.setData(baseGraph,{preserveView:true})
    s.finish()
    assert.equal(JSON.stringify(s.simulation.nodes().map(n=>[n.id,n.x,n.y])),positions)
  } finally {s.controller.destroy()}
})

test('入场运动结束不会再次改变比例，初次适配使用稳定布局',async()=>{
  const s=await scene()
  try {
    const initial={scale:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y}
    s.finish()
    assert.deepEqual({scale:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},initial)
    assert.ok(s.fitAlphas[0]<=s.simulation.alphaMin(),'首次适配前应先将初始布局求解至冷却阈值')
    assert.equal(s.transforms.length,1,'初始化只适配一次')
    s.controller.zoomBy(1.25)
    const userScale=s.stage.scale.x
    s.finish()
    assert.equal(s.stage.scale.x,userScale,'保留用户设置的缩放')
    s.controller.fit()
    assert.equal(s.transforms.length,3,'显式点击适应画布仍可重新计算')
  } finally {s.controller.destroy()}
})

test('加入历史关系后切页再进入，从缓存布局播放与首次一致的 D3 入场，保留视角',async()=>{
  const viewCache=modelModule.exports.graphViewCache('synthetic-route-return')
  const all={nodes:[...baseGraph.nodes,{id:'history',name:'合成历史实体',type:'entity'}],
    edges:[...baseGraph.edges,{source:'9',target:'11',predicate:'used_to',live:false},{source:'9',target:'history',predicate:'used_to',live:false}]}
  const first=await scene({viewCache})
  first.controller.setData(all)
  first.finish()
  first.controller.zoomBy(1.25)
  first.controller.panBy(30,-20)
  const positions=first.simulation.nodes().map(n=>({id:n.id,x:n.x,y:n.y}))
  const view={k:first.stage.scale.x,x:first.stage.position.x,y:first.stage.position.y}
  first.controller.destroy()
  const returned=await scene({data:all,viewCache})
  try {
    for (const [i,node] of returned.simulation.nodes().entries()) {
      assert.equal(node.x,positions[i].x*0.88,'从缓存布局按首次入场比例收拢')
      assert.equal(node.y,positions[i].y*0.88)
    }
    assert.deepEqual({k:returned.stage.scale.x,x:returned.stage.position.x,y:returned.stage.position.y},view)
    assert.equal(returned.restarts(),1,'返回时也启动真实 D3 受力动画')
    assert.equal(returned.simulation.alpha(),0.35,'返回与首次入场使用相同热度')
    returned.draw()
    const node=returned.simulation.nodes()[0]
    const gfx=returned.stage.children[1].children[0]
    assert.equal(gfx.position.x,node.x+450)
    assert.equal(gfx.position.y,node.y+250)
    const before=JSON.stringify(returned.simulation.nodes().map(n=>[n.x,n.y]))
    returned.controller.setSelected(node.id)
    returned.simulation.tick(20)
    assert.notEqual(JSON.stringify(returned.simulation.nodes().map(n=>[n.x,n.y])),before,'高亮后节点仍按受力自然运动')
    returned.controller.setData({nodes:[...all.nodes].reverse(),edges:[...all.edges].reverse()})
    assert.equal(returned.restarts(),1,'同一批数据重新读取或换序不重播动画')
    returned.finish()
    assert.deepEqual({k:returned.stage.scale.x,x:returned.stage.position.x,y:returned.stage.position.y},view)
    const previousModel=returned.simulation.nodes()
    returned.controller.reset()
    assert.notEqual(returned.simulation.nodes(),previousModel,'显式重置应忽略已缓存视图重新布局')
  } finally {returned.controller.destroy()}
})

test('返回动画中途离开不会积累收拢偏差，减少动画偏好直接恢复',async()=>{
  const viewCache=modelModule.exports.graphViewCache('synthetic-return-motion')
  const first=await scene({viewCache})
  first.finish()
  const positions=JSON.stringify(first.simulation.nodes().map(n=>[n.id,n.x,n.y]))
  first.controller.destroy()
  for(let i=0;i<3;i++) {
    const returning=await scene({viewCache})
    returning.simulation.tick(20)
    returning.draw()
    returning.controller.destroy()
  }
  const reduced=await scene({viewCache,reducedMotion:true})
  try {
    reduced.draw()
    assert.equal(JSON.stringify(reduced.simulation.nodes().map(n=>[n.id,n.x,n.y])),positions)
    const node=reduced.simulation.nodes()[0],gfx=reduced.stage.children[1].children[0]
    assert.equal(gfx.position.x,node.x+450)
    assert.equal(gfx.position.y,node.y+250)
    assert.equal(reduced.restarts(),0)
  } finally {reduced.controller.destroy()}
})

test('视图只在相同身份、图数据和受力配置下复用，缓存有界',()=>{
  const {graphViewSignature,graphViewCache}=modelModule.exports
  const settings={nodeScale:1.4,repelForce:1,centerForce:0.2,linkDistance:110,linkForce:0.6}
  const signature=graphViewSignature(baseGraph,settings)
  assert.equal(signature,graphViewSignature({nodes:[...baseGraph.nodes].reverse(),edges:[...baseGraph.edges].reverse()},settings))
  assert.notEqual(signature,graphViewSignature(baseGraph,{...settings,repelForce:2}))
  assert.notEqual(signature,graphViewSignature({...baseGraph,edges:baseGraph.edges.slice(1)},settings))
  const a=graphViewCache('synthetic-cache-a'),b=graphViewCache('synthetic-cache-b')
  const view={signature,nodes:[],width:900,height:500,transform:{k:1,x:0,y:0}}
  a.set(view)
  assert.equal(a.get(signature),view)
  assert.equal(b.get(signature),undefined)
  for(let i=0;i<25;i++) a.set({...view,signature:`synthetic-${i}`})
  assert.equal(a.get(signature),undefined)
})

test('正常入场有节点运动，减少动画偏好下保持静态',async()=>{
  const animated=await scene()
  const reduced=await scene({reducedMotion:true})
  try {
    assert.equal(animated.restarts(),1,'正常入场应启动 D3 运动')
    const before=JSON.stringify(animated.simulation.nodes().map(n=>[n.x,n.y]))
    animated.simulation.tick(20)
    assert.notEqual(JSON.stringify(animated.simulation.nodes().map(n=>[n.x,n.y])),before,'节点应继续自然运动')
    assert.equal(reduced.restarts(),0,'减少动画偏好下不启动入场模拟')
    assert.ok(reduced.simulation.alpha()<=reduced.simulation.alphaMin())
  } finally {animated.controller.destroy();reduced.controller.destroy()}
})

test('仅当前只切换节点与连线显隐，反复切换保持坐标、受力、相机及入场次数',async()=>{
  const data={nodes:[...baseGraph.nodes,{id:'history',name:'合成历史实体',type:'entity'}],
    edges:[...baseGraph.edges,{source:'9',target:'11',predicate:'used_to',live:false},{source:'9',target:'history',predicate:'used_to',live:false}]}
  const s=await scene({data,liveOnly:true})
  try {
    s.finish();s.draw(500)
    const nodes=s.simulation.nodes(),positions=JSON.stringify(nodes.map(n=>[n.id,n.x,n.y]))
    const physics=s.simulation.force('link').links()
    s.controller.zoomBy(1.25);s.controller.panBy(30,-20)
    const view={k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},restarts=s.restarts(),transforms=s.transforms.length
    const graphics=s.stage.children[1].children,links=s.stage.children[0].children
    assert.equal(graphics.at(-1).visible,false,'初次仅当前也应隐藏历史独有节点')
    assert.equal(links.at(-1).visible,false)
    for (const liveOnly of [false,true,false,true]) {
      s.controller.setLiveOnly(liveOnly);s.draw(500)
      assert.equal(graphics.at(-1).visible,!liveOnly)
      assert.equal(links.at(-1).visible,!liveOnly)
      assert.equal(graphics[0].visible,true)
      assert.equal(s.simulation.nodes(),nodes,'不重建模型')
      assert.equal(s.simulation.force('link').links(),physics,'不重配受力')
      assert.equal(JSON.stringify(nodes.map(n=>[n.id,n.x,n.y])),positions)
      assert.equal(s.restarts(),restarts,'不重播动画或唤醒模拟')
      assert.equal(s.transforms.length,transforms,'不重新适配视口')
      assert.deepEqual({k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},view)
    }
  } finally {s.controller.destroy()}
})

test('全部关系被隐藏时节点不可命中，关闭仅当前后恢复命中',async()=>{
  const data={nodes:[{id:'a',name:'合成甲',type:'entity'},{id:'b',name:'合成乙',type:'entity'}],
    edges:[{source:'a',target:'b',predicate:'used_to',live:false}]}
  const s=await scene({data,liveOnly:true,reducedMotion:true})
  try {
    s.draw()
    const node=s.simulation.nodes()[0]
    assert.equal(s.hitNode(node),undefined,'隐藏节点不阻挡画布点击，也不可拖动')
    assert.ok(s.stage.children[1].children.every(gfx=>gfx.visible===false))
    s.controller.setLiveOnly(false);s.draw()
    assert.equal(s.hitNode(node),'a')
    assert.ok(s.stage.children[1].children.every(gfx=>gfx.visible===true))
  } finally {s.controller.destroy()}
})

test('重置和适应画布共用最终范围，动画结束不再改变视角',async()=>{
  const s=await scene()
  try {
    s.finish()
    s.controller.zoomBy(1.25)
    s.controller.panBy(40,-20)
    s.controller.reset()
    const startView={k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},transforms=s.transforms.length
    s.finish()
    const resetView={k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y}
    assert.deepEqual(resetView,startView,'重置结束时不再补一次 fit')
    assert.equal(s.transforms.length,transforms)
    s.controller.fit()
    assert.deepEqual({k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},resetView,
      '重置的最终适配应使用动画稳定后的节点范围')
  } finally {s.controller.destroy()}
})

test('适应画布以当前设置重新排列并展开，保留显隐和选中实体',async()=>{
  const data={nodes:[...baseGraph.nodes,{id:'old',name:'历史实体',type:'entity'}],
    edges:[...baseGraph.edges,{source:'0',target:'old',predicate:'used_to',live:false}]}
  const settings={repelForce:1.7,linkDistance:140,linkForce:.8,nodeScale:1.6,lineWidth:1.2,showRelations:true}
  const s=await scene({data,liveOnly:true,settings,selectedId:'0'})
  try {
    s.finish()
    const oldNodes=s.simulation.nodes()
    oldNodes[0].x+=100
    s.controller.fit()
    assert.notEqual(s.simulation.nodes(),oldNodes,'适应画布现在会重新计算布局')
    assert.equal(s.simulation.force('charge').strength()(s.simulation.nodes()[0]),-170)
    assert.equal(s.simulation.force('link').distance()(s.simulation.force('link').links()[0]),140)
    assert.equal(s.simulation.force('link').strength()(s.simulation.force('link').links()[0]),.8)
    s.draw()
    assert.equal(s.stage.children[1].children.at(-1).visible,false)
    assert.equal(s.stage.children[2].children[0].style.fill,'#edf3fa','适应画布保留选择')
    assert.equal(s.stage.children[2].children[data.nodes.length].visible,true,'保留关系名称设置')
    const before=JSON.stringify(s.simulation.nodes().map(n=>[n.x,n.y]))
    const view={k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},transforms=s.transforms.length
    s.finish()
    assert.notEqual(JSON.stringify(s.simulation.nodes().map(n=>[n.x,n.y])),before,'有展开动画')
    assert.deepEqual({k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},view)
    assert.equal(s.transforms.length,transforms,'动画结束相机不跳动')
    s.controller.reset();s.draw()
    assert.equal(s.simulation.force('link').distance()(s.simulation.force('link').links()[0]),110,'重置才恢复默认参数')
    assert.equal(s.stage.children[2].children[data.nodes.length].visible,false,'重置恢复默认显示参数')
    assert.equal(s.stage.children[2].children[0].style.fill,'#9baeca','重置清除选择')
    assert.equal(s.stage.children[1].children.at(-1).visible,false,'重置保留筛选')
  } finally {s.controller.destroy()}
})

test('按钮展开期间的缩放、平移或再次适配不被轨迹播放改写',async()=>{
  for (const action of [s=>s.controller.zoomBy(1.25),s=>s.controller.panBy(40,-20),s=>s.controller.fit(),s=>s.wheelZoom(1.25)]) {
    const s=await scene()
    try {
      s.finish();s.controller.reset()
      s.draw(500)
      action(s)
      const view={k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y}
      s.finish()
      assert.deepEqual({k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},view)
    } finally {s.controller.destroy()}
  }
})

test('减少动画模式重置立即完成适配，结束回调不再次改变视角',async()=>{
  const s=await scene({reducedMotion:true})
  try {
    s.controller.reset()
    const view={k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},transforms=s.transforms.length
    s.simulation.on('end.render')?.()
    assert.equal(s.transforms.length,transforms)
    s.controller.fit()
    assert.deepEqual({k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},view)
  } finally {s.controller.destroy()}
})

test('按钮播放中的拖动和作用力调节从可见位置接管，旧轨迹不再写回',async()=>{
  for (const action of ['drag','configure']) {
    const s=await scene()
    try {
      s.finish();s.controller.fit();s.draw(500)
      const node=s.simulation.nodes()[0],positions=JSON.stringify(s.simulation.nodes().map(n=>[n.x,n.y]))
      if (action==='drag') s.dragStart(node)
      else s.controller.configure({nodeScale:1.4,lineWidth:.8,repelForce:2,centerForce:.2,linkDistance:110,linkForce:.6,showRelations:false,opacityScale:3})
      s.draw(10_000)
      assert.equal(JSON.stringify(s.simulation.nodes().map(n=>[n.x,n.y])),positions,'没有跳到旧轨迹终点')
      s.simulation.tick(20)
      assert.notEqual(JSON.stringify(s.simulation.nodes().map(n=>[n.x,n.y])),positions,'实时 D3 已接管')
      if (action==='drag') s.dragEnd(node)
    } finally {s.controller.destroy()}
  }
})

test('预计算轨迹使用真实受力，插值终点准确，120 节点帧数有界',()=>{
  const {createQuartzModel,createQuartzSimulation,prepareEntrance,applyEntrance}=modelModule.exports
  const data={nodes:Array.from({length:120},(_,i)=>({id:String(i),name:`合成 ${i}`,type:'entity'})),
    edges:Array.from({length:119},(_,i)=>({source:'0',target:String(i+1),predicate:'related_to',live:true}))}
  const model=createQuartzModel(data),simulation=createQuartzSimulation(model,{nodeScale:1.4,repelForce:1,centerForce:.2,linkDistance:110,linkForce:.6})
  try {
    simulation.alpha(1).tick(300)
    const baseline=model.nodes.map(n=>[n.x,n.y])
    const entrance=prepareEntrance(simulation,model.nodes)
    const final=JSON.stringify(model.nodes.map(n=>[n.x,n.y]))
    assert.ok(entrance.frames.length>2 && entrance.frames.length<=300)
    assert.ok(entrance.frames.reduce((sum,frame)=>sum+frame.byteLength,0)<600_000)
    assert.ok(simulation.alpha()<simulation.alphaMin())
    assert.equal(applyEntrance(model.nodes,entrance,-1),false)
    assert.equal(model.nodes[0].x,baseline[0][0]*.88)
    applyEntrance(model.nodes,entrance,entrance.duration/2)
    assert.ok(model.nodes.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)))
    assert.notEqual(JSON.stringify(model.nodes.map(n=>[n.x,n.y])),final)
    assert.equal(applyEntrance(model.nodes,entrance,entrance.duration+1),true)
    assert.equal(JSON.stringify(model.nodes.map(n=>[n.x,n.y])),final,'最后一帧精确回到用于适配的范围')
  } finally {simulation.stop()}
})

test('长名称总览截短，缩远淡出，鼠标和键盘聚焦显示全名，离开恢复选择',async()=>{
  const fullName='合成实体 🔭 '.repeat(8)
  const data={...baseGraph,nodes:baseGraph.nodes.map(n=>n.id==='0'?{...n,name:fullName}:n)}
  for (const reducedMotion of [false,true]) {
    const s=await scene({data,reducedMotion})
    try {
      s.finish();s.draw(500)
      const label=s.stage.children[2].children[0],gfx=s.stage.children[1].children[0]
      const overview=label.text,alpha=label.alpha
      assert.ok(overview.endsWith('…') && overview.length<fullName.length)
      assert.ok(label.getLocalBounds().width<=120)
      assert.ok(!overview.includes('\uFFFD'),'截短不能破坏 Unicode 码点')
      s.controller.zoomBy(.25);s.draw(500)
      assert.ok(label.alpha<alpha,'缩远时普通标签更早淡出')
      gfx.handlers.pointerover();s.draw(500)
      assert.equal(label.text,fullName)
      assert.equal(label.alpha,1,'悬停名称不受缩放淡出影响')
      gfx.handlers.pointerleave();s.draw(500)
      assert.equal(label.text,overview)
      s.controller.setSelected('0');s.controller.setFocus('1');s.draw(500)
      assert.equal(label.text,overview,'键盘焦点临时接管阅读层级')
      s.controller.setFocus(null);s.draw(500)
      assert.equal(label.text,fullName,'离开临时焦点后恢复选择')
      s.controller.setSelected(null);s.draw(500)
      assert.equal(label.text,overview)
      assert.equal(data.nodes[0].name,fullName,'完整数据不被截短')
    } finally {s.controller.destroy()}
  }
})

test('聚焦区分当前节点、直接邻居和无关对象，退出恢复且不改变布局或相机',async()=>{
  const data={...baseGraph,edges:[...baseGraph.edges,{source:'0',target:'9',predicate:'used_to',live:false}]}
  const s=await scene({data,reducedMotion:true,liveOnly:true})
  try {
    s.draw()
    const positions=JSON.stringify(s.simulation.nodes().map(n=>[n.x,n.y])),transforms=s.transforms.length,restarts=s.restarts()
    const nodes=s.stage.children[1].children,labels=s.stage.children[2].children,links=s.stage.children[0].children
    const normalAlpha=labels[9].alpha,normalStroke=links[0].lastStroke.width
    s.controller.setSelected('0');s.draw()
    assert.ok(nodes[0].alpha>nodes[1].alpha && nodes[1].alpha>nodes[9].alpha)
    assert.ok(labels[0].alpha>labels[1].alpha && labels[1].alpha>labels[9].alpha)
    assert.ok(labels[0].zIndex>labels[1].zIndex && labels[1].zIndex>labels[9].zIndex)
    assert.ok(links[0].lastStroke.width>normalStroke)
    assert.ok(links[0].lastStroke.alpha>links[8].lastStroke.alpha)
    assert.ok(labels[9].alpha<normalAlpha,'历史邻居不能混入仅当前高亮')
    s.controller.setLiveOnly(false);s.draw()
    assert.ok(labels[9].alpha>normalAlpha,'展示历史后它成为可见邻居')
    s.controller.setSelected(null);s.draw()
    assert.ok(nodes.every(n=>n.alpha===1))
    assert.equal(labels[9].alpha,normalAlpha)
    assert.equal(links[0].lastStroke.width,normalStroke)
    assert.equal(JSON.stringify(s.simulation.nodes().map(n=>[n.x,n.y])),positions)
    assert.equal(s.transforms.length,transforms)
    assert.equal(s.restarts(),restarts)
  } finally {s.controller.destroy()}
})

test('展开长名称不改变适配结果，详情全名不会挤小图谱',async()=>{
  const data={...baseGraph,nodes:baseGraph.nodes.map(n=>({...n,name:'合成名称'.repeat(30)}))}
  const s=await scene({data,reducedMotion:true})
  try {
    const view={k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y}
    s.controller.setSelected('0');s.draw()
    s.controller.fit()
    assert.deepEqual({k:s.stage.scale.x,x:s.stage.position.x,y:s.stage.position.y},view)
    assert.equal(s.stage.children[2].children[0].text,data.nodes[0].name)
  } finally {s.controller.destroy()}
})
