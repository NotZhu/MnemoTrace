const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const reactQuery = require('@tanstack/react-query')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/hooks/queries.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
  external: ['@tanstack/react-query', '../lib/api', '../store/auth'],
}).outputFiles[0].text

function setup(resource = 'memories', options = {keepPreviousData:true}) {
  let namespace = 'synthetic-space-a'
  const useAuth = selector => selector({apiKey:namespace})
  useAuth.getState = () => ({apiKey:namespace})
  const moduleData = {exports:{}}
  vm.runInNewContext(source,{module:moduleData,exports:moduleData.exports,require:name=>{
    if(name==='@tanstack/react-query') return {...reactQuery,useQuery:options=>options}
    if(name==='../store/auth') return {useAuth}
    if(name==='../lib/api') return {api:{memories:()=>new Promise(()=>{}),sessions:()=>new Promise(()=>{})}}
    throw new Error(name)
  }})
  const hooks = moduleData.exports
  const client = new reactQuery.QueryClient({defaultOptions:{queries:{staleTime:Infinity,retry:false,gcTime:0}}})
  const data=resource==='sessions'?{sessions:[{id:'synthetic-session'}]}:{facts:[{id:'synthetic-fact'}],episodes:[]}
  const initial=resource==='sessions'?{limit:12,q:'before'}:{kind:'outcomes',q:'before'}
  client.setQueryData(hooks.qk[resource](initial),data)
  const hook=resource==='sessions'?hooks.useSessions:hooks.useMemories
  const observer=new reactQuery.QueryObserver(client,hook(initial,options))
  const unsubscribe=observer.subscribe(()=>{})
  return {observer,data,hooks,client,setNamespace:value=>{namespace=value},cleanup:()=>{unsubscribe();observer.destroy();client.clear()}}
}

test('搜索新词时保留同一空间的上一页结果，不进入整页首次加载状态',()=>{
  const s=setup()
  try {
    s.observer.setOptions(s.hooks.useMemories({kind:'outcomes',q:'after'},{keepPreviousData:true}))
    const result=s.observer.getCurrentResult()
    assert.equal(result.data,s.data)
    assert.equal(result.isPlaceholderData,true)
    assert.equal(result.isLoading,false)
    assert.equal(result.isFetching,true)
  } finally {s.cleanup()}
})
test('切换记忆空间时不显示上一空间的搜索结果',()=>{
  const s=setup()
  try {
    s.setNamespace('synthetic-space-b')
    s.observer.setOptions(s.hooks.useMemories({kind:'outcomes',q:'after'},{keepPreviousData:true}))
    assert.equal(s.observer.getCurrentResult().data,undefined)
    assert.equal(s.observer.getCurrentResult().isPlaceholderData,false)
  } finally {s.cleanup()}
})
test('切换记录类型和未启用选项的调用不保留旧结果',()=>{
  for(const enabled of [true,false]) {
    const s=setup()
    try {
      s.observer.setOptions(s.hooks.useMemories({q:'after'},enabled?{keepPreviousData:true}:undefined))
      assert.equal(s.observer.getCurrentResult().data,undefined)
    } finally {s.cleanup()}
  }
})

test('会话搜索请求期间保留列表，结果到达后替换列表',()=>{
  const s=setup('sessions')
  try {
    const next={limit:12,q:'after'}
    s.observer.setOptions(s.hooks.useSessions(next,{keepPreviousData:true}))
    assert.equal(s.observer.getCurrentResult().data,s.data)
    assert.equal(s.observer.getCurrentResult().isLoading,false)
    assert.equal(s.observer.getCurrentResult().isFetching,true)
    const updated={sessions:[{id:'synthetic-match'}]}
    s.client.setQueryData(s.hooks.qk.sessions(next),updated)
    assert.equal(s.observer.getCurrentResult().data,updated)
    assert.equal(s.observer.getCurrentResult().isPlaceholderData,false)
  } finally {s.cleanup()}
})

test('会话搜索切换空间时不沿用其他空间的列表',()=>{
  const s=setup('sessions')
  try {
    s.setNamespace('synthetic-space-b')
    s.observer.setOptions(s.hooks.useSessions({q:'after'},{keepPreviousData:true}))
    assert.equal(s.observer.getCurrentResult().data,undefined)
  } finally {s.cleanup()}
})

test('未启用保留结果选项的会话查询保持原有加载行为',()=>{
  const s=setup('sessions',{})
  try {
    s.observer.setOptions(s.hooks.useSessions({q:'after'}))
    assert.equal(s.observer.getCurrentResult().data,undefined)
    assert.equal(s.observer.getCurrentResult().isLoading,true)
  } finally {s.cleanup()}
})
