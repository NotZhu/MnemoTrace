const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const {createHash,webcrypto} = require('node:crypto')
const esbuild = require('esbuild')

const source=esbuild.buildSync({
  entryPoints:[path.resolve(__dirname,'../src/store/preferences.ts')],
  bundle:true,platform:'browser',format:'cjs',write:false,
  external:['react','./auth','../lib/api'],
}).outputFiles[0].text
const KEY='mnemotrace.preferences'
function load(saved,{unavailable=false,noCrypto=false,cryptoImpl=webcrypto}={}) {
  const values=new Map(saved===undefined?[]:[[KEY,typeof saved==='string'?saved:JSON.stringify(saved)]])
  const listeners=[],timers=new Map(),events={}
  let auth={apiKey:null,epoch:0},timerId=0,writes=0
  const useAuth=()=>{}
  useAuth.getState=()=>auth
  useAuth.subscribe=callback=>{listeners.push(callback);return()=>{}}
  const window={addEventListener(name,callback) {events[name]=callback}}
  Object.defineProperty(window,'localStorage',{get() {
    if(unavailable) throw new Error('storage unavailable')
    return {getItem:key=>values.get(key)??null,setItem(key,value) {writes++;values.set(key,value)},removeItem:key=>values.delete(key)}
  }})
  const moduleData={exports:{}}
  vm.runInNewContext(source,{
    module:moduleData,exports:moduleData.exports,window,crypto:noCrypto?undefined:cryptoImpl,TextEncoder,
    setTimeout(callback) {timers.set(++timerId,callback);return timerId},clearTimeout:id=>timers.delete(id),
    require(name) {
      if(name==='./auth') return {useAuth,
        currentSession:()=>({key:auth.apiKey,epoch:auth.epoch}),
        isCurrentSession:session=>session.key===auth.apiKey&&session.epoch===auth.epoch}
      if(name==='../lib/api') return {apiBaseUrl:()=> 'https://synthetic.invalid'}
      return require(name)
    },
  })
  const exports=moduleData.exports,store=exports.usePreferences
  return {store,values,exports,events,writes:()=>writes,
    state:()=>store.getState(),flush:exports.flushPreferences,
    async login(key) {
      const old=auth;auth={apiKey:key,epoch:old.epoch+1};listeners.forEach(fn=>fn(auth,old))
      if(key) await exports.preferenceScopeFor(key)
      await Promise.resolve()
      return store.getState().activeScope
    },
    startLogin(key) {const old=auth;auth={apiKey:key,epoch:old.epoch+1};listeners.forEach(fn=>fn(auth,old))},
  }
}
const savedState=s=>JSON.parse(s.values.get(KEY)).state
const savedVersion=s=>JSON.parse(s.values.get(KEY)).version

test('图谱即时更新、合并写入，离页保存且重新加载恢复，重置也持久化',()=>{
  const s=load()
  s.state().setGraph({nodeScale:1.6})
  s.state().setGraph({nodeScale:1.7,lineWidth:1.1})
  assert.equal(s.state().graph.nodeScale,1.7)
  assert.equal(s.writes(),0)
  s.events.pagehide()
  assert.equal(s.writes(),1)
  const reload=load(s.values.get(KEY))
  assert.equal(reload.state().graph.nodeScale,1.7)
  assert.equal(reload.state().graph.lineWidth,1.1)
  reload.state().resetGraph()
  assert.equal(load(reload.values.get(KEY)).state().graph.nodeScale,1.4)
})

test('所有长期筛选立即保存，页面重建/刷新可恢复，账号和服务地址隔离且不保存明文凭据',async()=>{
  const s=load(),credential='synthetic-credential-a'
  const a=await s.login(credential)
  s.state().setSpace('graph','liveOnly',false)
  s.state().setSpace('graph','hideSensitive',true)
  s.state().setSpace('facts','status','live')
  s.state().setSpace('facts','hideSensitive',true)
  s.state().setSpace('facts','topic','food')
  s.state().setSpace('journal','topic','unknown')
  s.state().setSpace('journal','showSuperseded',true)
  s.state().setSpace('ask','chunks',9)
  s.state().setSpace('ask','redactSensitive',true)
  const raw=s.values.get(KEY)
  assert.ok(!raw.includes(credential))
  assert.ok(!raw.includes('activeScope'))
  const reload=load(raw)
  assert.equal(await reload.login(credential),a)
  assert.equal(reload.state().spaces[a].ask.chunks,9)
  assert.equal(reload.state().spaces[a].journal.showSuperseded,true)
  assert.equal(reload.state().spaces[a].facts.hideSensitive,true)
  assert.equal(reload.state().spaces[a].facts.topic,'food')
  assert.equal(reload.state().spaces[a].journal.topic,'unknown')
  assert.equal(reload.state().spaces[a].facts.status,'live')
  assert.equal(reload.state().spaces[a].graph.liveOnly,false)
  assert.equal(reload.state().spaces[a].graph.hideSensitive,true)
  assert.equal(reload.state().spaces[a].ask.redactSensitive,true)
  const b=await reload.login('synthetic-credential-b')
  assert.notEqual(a,b)
  assert.equal(reload.state().spaces[b],undefined)
  reload.state().setSpace('ask','chunks',3)
  await reload.login(credential)
  assert.equal(reload.state().spaces[a].ask.chunks,9)
  assert.notEqual(await reload.exports.preferenceScopeFor(credential,'https://other.invalid'),a)
})

test('指纹未完成时的修改不会丢失，快速换账号不会使用旧空间',async()=>{
  const s=load()
  s.startLogin('synthetic-race-a')
  s.state().setSpace('facts','status','live')
  const a=await s.exports.preferenceScopeFor('synthetic-race-a')
  await Promise.resolve()
  assert.equal(s.state().spaces[a].facts.status,'live')
  s.startLogin('synthetic-race-b')
  assert.equal(s.state().activeScope,null)
  await s.login('synthetic-race-c')
  const current=s.state().activeScope
  await s.exports.preferenceScopeFor('synthetic-race-b')
  assert.equal(s.state().activeScope,current)
  await s.login(null)
  s.state().setSpace('ask','chunks',8)
  assert.equal(s.state().activeScope,null)
})

test('仅允许声明的偏好，缓存类型/范围异常或损坏时使用默认值',async()=>{
  const base=load(),scope=await base.login('synthetic-validation')
  const s=load({version:2,state:{
    graph:{nodeScale:999,lineWidth:0.7,showRelations:'true',enableRadial:true},
    spaces:{[scope]:{ask:{chunks:99,redactSensitive:true},facts:{status:'invalid',topic:'Music',undeclared:'x'},journal:{topic:['food']},query:'不应保存的搜索'},'raw-credential':{ask:{chunks:4}}},
    apiKey:'must-not-restore',query:'must-not-restore',
  }})
  assert.equal(s.state().graph.nodeScale,1.4)
  assert.equal(s.state().graph.lineWidth,0.7)
  assert.equal(s.state().graph.showRelations,false)
  assert.equal(s.state().graph.enableRadial,undefined)
  assert.equal(s.state().spaces[scope].ask.chunks,6)
  assert.equal(s.state().spaces[scope].ask.redactSensitive,true)
  assert.equal(s.state().spaces[scope].facts.status,'live')
  assert.equal(s.state().spaces[scope].facts.topic,'')
  assert.equal(s.state().spaces[scope].journal.topic,'')
  s.flush()
  assert.ok(!s.values.get(KEY).includes('must-not-restore'))
  assert.ok(!s.values.get(KEY).includes('不应保存的搜索'))
  assert.equal(load('{bad json').state().graph.nodeScale,1.4)
  assert.equal(load({version:2,state:null}).state().graph.nodeScale,1.4)
  assert.equal(load({version:2,state:{spaces:null}}).state().graph.nodeScale,1.4)
})

test('旧版本缓存整份重置为默认值并回写当前版本',async()=>{
  const legacyScope=`space-${'a'.repeat(64)}`
  for(const version of [0,1]) {
    const legacy={graph:{nodeScale:1.6,showRelations:true},spaces:{[legacyScope]:{facts:{status:'all',hideSensitive:true},ask:{chunks:9}}}}
    const s=load({version,state:legacy})
    assert.equal(s.state().graph.nodeScale,1.4,'旧版本不保留图谱参数')
    assert.equal(s.state().graph.showRelations,false)
    assert.deepEqual(Object.keys(s.state().spaces),[],'旧版本不保留空间筛选')
    s.flush()
    assert.equal(savedVersion(s),2,'重置后回写当前版本')
    assert.ok(!s.values.get(KEY).includes('"nodeScale":1.6'))
    assert.ok(!s.values.get(KEY).includes('"chunks":9'))
  }
  assert.equal(load({version:50,state:{graph:{nodeScale:1.6}}}).state().graph.nodeScale,1.4)
})

test('旧版包含历史开关不再转换成三态筛选',async()=>{
  const base=load(),credential='synthetic-legacy-show-old',scope=await base.login(credential)
  for(const showOld of [true,false]) {
    const s=load({version:1,state:{spaces:{[scope]:{facts:{showOld,hideSensitive:true}}}}})
    await s.login(credential)
    assert.equal(s.state().spaces[scope],undefined,'旧版本空间偏好整份丢弃')
    s.flush()
    assert.equal(savedVersion(s),2)
    assert.ok(!s.values.get(KEY).includes('showOld'))
  }
  const current=load({version:2,state:{spaces:{[scope]:{facts:{showOld:true}}}}})
  await current.login(credential)
  assert.equal(current.state().spaces[scope].facts.status,'live','未声明字段回落到默认值')
  current.state().setSpace('facts','hideSensitive',true)
  current.flush()
  assert.ok(!savedState(current).spaces[scope].facts.showOld,'下次写入不带未声明字段')
})

test('旧版本重置后仍可写入并在刷新后恢复',async()=>{
  const base=load(),credential='synthetic-reset-writable',scope=await base.login(credential)
  const s=load({version:1,state:{spaces:{[scope]:{ask:{chunks:9},facts:{status:'all'}}}}})
  await s.login(credential)
  assert.equal(s.state().spaces[scope],undefined)
  s.state().setSpace('ask','chunks',8)
  s.state().setSpace('facts','status','superseded')
  const reloaded=load(s.values.get(KEY))
  await reloaded.login(credential)
  assert.equal(reloaded.state().spaces[scope].ask.chunks,8)
  assert.equal(reloaded.state().spaces[scope].facts.status,'superseded')
  await reloaded.login('synthetic-reset-other')
  assert.equal(reloaded.state().spaces[reloaded.state().activeScope],undefined,'重置不破坏账号隔离')
})

test('问答片段最小值为2，当前格式的越界输入被钳制并保持其他设置',async()=>{
  const base=load(),credential='synthetic-chunk-minimum',scope=await base.login(credential)
  const s=load({version:2,state:{spaces:{[scope]:{ask:{chunks:1,redactSensitive:true},facts:{hideSensitive:true}}}}})
  await s.login(credential)
  assert.equal(s.state().spaces[scope].ask.chunks,2)
  assert.equal(s.state().spaces[scope].ask.redactSensitive,true)
  assert.equal(s.state().spaces[scope].facts.hideSensitive,true)
  s.state().setSpace('ask','chunks',1)
  assert.equal(savedState(s).spaces[scope].ask.chunks,2)
  s.state().setSpace('ask','chunks',previous=>previous-1)
  assert.equal(s.state().spaces[scope].ask.chunks,2)
  s.state().setSpace('ask','chunks',12)
  const reloaded=load(s.values.get(KEY))
  await reloaded.login(credential)
  assert.equal(reloaded.state().spaces[scope].ask.chunks,12)
})

test('事实分类与三态筛选按空间持久化',async()=>{
  const base=load(),credential='synthetic-fact-filters',scope=await base.login(credential)
  for(const status of ['live','superseded','all']) {
    const s=load({version:2,state:{spaces:{[scope]:{facts:{status,hideSensitive:true}}}}})
    await s.login(credential)
    assert.equal(s.state().spaces[scope].facts.status,status)
    assert.equal(s.state().spaces[scope].facts.hideSensitive,true)
    s.state().setSpace('facts','status','superseded')
    s.state().setSpace('facts','category','合成分类')
    const raw=s.values.get(KEY),reloaded=load(raw)
    await reloaded.login(credential)
    assert.equal(reloaded.state().spaces[scope].facts.status,'superseded')
    assert.equal(reloaded.state().spaces[scope].facts.category,'合成分类')
    assert.ok(!raw.includes('showOld'))
    await reloaded.login('synthetic-other-fact-filters')
    assert.equal(reloaded.state().spaces[reloaded.state().activeScope],undefined)
  }
  const invalid=load({version:2,state:{spaces:{[scope]:{facts:{status:'invalid',category:[],hideSensitive:false}}}}})
  assert.equal(invalid.state().spaces[scope].facts.status,'live')
  assert.equal(invalid.state().spaces[scope].facts.category,'')
})

test('存储不可用时仍能修改内存中的偏好',async()=>{
  const s=load(undefined,{unavailable:true})
  const scope=await s.login('synthetic-unavailable')
  s.state().setSpace('ask','chunks',10)
  s.state().setGraph({showRelations:true})
  assert.equal(s.state().spaces[scope].ask.chunks,10)
  assert.equal(s.state().graph.showRelations,true)
})

const cryptoFallbacks = [
  ['crypto 不存在',{noCrypto:true}],
  ['subtle 不存在',{cryptoImpl:{}}],
  ['原生 digest 拒绝',{cryptoImpl:{subtle:{digest:async()=>{throw new Error('digest unavailable')}}}}],
]
for(const [label,options] of cryptoFallbacks) {
  test(`${label}时偏好可跨刷新恢复，账号/服务隔离且不保存明文 key`,async()=>{
    const credential='synthetic-http-credential-a',server='https://synthetic.invalid'
    const expected='space-'+createHash('sha256').update(JSON.stringify([server,credential]),'utf8').digest('hex')
    const original=load(undefined,options),scope=await original.login(credential)
    assert.equal(scope,expected)
    original.state().setSpace('facts','status','all')
    original.state().setSpace('facts','hideSensitive',true)
    original.state().setSpace('ask','chunks',9)
    const raw=original.values.get(KEY)
    assert.ok(!raw.includes(credential))
    assert.ok(!raw.includes(server))
    assert.deepEqual(Object.keys(savedState(original).spaces),[scope])
    const reload=load(raw,options)
    assert.equal(await reload.login(credential),scope)
    assert.equal(reload.state().spaces[scope].facts.status,'all')
    assert.equal(reload.state().spaces[scope].facts.hideSensitive,true)
    assert.equal(reload.state().spaces[scope].ask.chunks,9)
    const other=await reload.login('synthetic-http-credential-b')
    assert.notEqual(other,scope)
    assert.equal(reload.state().spaces[other],undefined)
    assert.notEqual(await reload.exports.preferenceScopeFor(credential,'http://other.invalid'),scope)
    assert.equal(await reload.login(credential),scope)
    assert.equal(reload.state().spaces[scope].ask.chunks,9)
    // 原生与回退实现可相互读取已有缓存；同版本下存储键与字段格式一致。
    const native=load(raw)
    assert.equal(await native.login(credential),scope)
    assert.equal(native.state().spaces[scope].facts.status,'all')
    native.state().setSpace('ask','chunks',4)
    const fallback=load(native.values.get(KEY),options)
    assert.equal(await fallback.login(credential),scope)
    assert.equal(fallback.state().spaces[scope].ask.chunks,4)
  })
}

test('回退和原生 SHA-256 对 UTF-8、长输入与分隔符得到相同编号',async()=>{
  const native=load(),fallback=load(undefined,{noCrypto:true})
  const cases=[
    ['synthetic-ascii','http://synthetic.invalid'],
    ['synthetic-quoted-"key\\','https://synthetic.invalid/示例/🌱'],
    ['synthetic-long-'+ 'x'.repeat(1024),'https://synthetic.invalid'],
  ]
  for(const [credential,server] of cases) {
    const expected='space-'+createHash('sha256').update(JSON.stringify([server,credential]),'utf8').digest('hex')
    assert.equal(await native.exports.preferenceScopeFor(credential,server),expected)
    assert.equal(await fallback.exports.preferenceScopeFor(credential,server),expected)
  }
})

test('快速连续切换用最新状态计算；问答数值校验，不持久化会话/结果/草稿',async()=>{
  const s=load(),scope=await s.login('synthetic-updates')
  s.state().setSpace('graph','liveOnly',previous=>!previous)
  s.state().setSpace('graph','liveOnly',previous=>!previous)
  assert.equal(s.state().spaces[scope].graph.liveOnly,true)
  s.state().setSpace('ask','chunks',1.5)
  assert.equal(s.state().spaces[scope].ask.chunks,6)
  assert.deepEqual(Object.keys(savedState(s)).sort(),['graph','spaces'])
  assert.deepEqual(Object.keys(savedState(s).spaces[scope]).sort(),['ask','facts','graph','journal'])
})

for (const relogin of ['logout-login', 'same-key-login']) {
  test(`延迟偏好修改不会穿过 ${relogin}，新周期修改仍生效`,async()=>{
    let finish
    const s=load(undefined,{cryptoImpl:{subtle:{digest:()=>new Promise(resolve=>{finish=resolve})}}})
    const credential='synthetic-pending-preferences'
    s.startLogin(credential)
    s.state().setSpace('ask','chunks',9)
    if(relogin==='logout-login') s.startLogin(null)
    s.startLogin(credential)
    assert.equal(s.state().activeScope,null)
    s.state().setSpace('graph','hideSensitive',true)
    finish(new Uint8Array(32).buffer)
    await s.exports.preferenceScopeFor(credential)
    await Promise.resolve()
    const state=s.state(),preferences=state.spaces[state.activeScope]
    assert.equal(preferences.ask.chunks,6,'旧周期的修改必须丢弃')
    assert.equal(preferences.graph.hideSensitive,true,'当前周期的修改仍应保存')
  })
}
