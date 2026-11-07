const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const rq = require('@tanstack/react-query')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/hooks/queries.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
  external: ['@tanstack/react-query', '../lib/api', '../store/auth'],
}).outputFiles[0].text
const initial = {q:'',live_only:false,include_sensitive:true,limit:120}
const full = {nodes:[{id:'synthetic-private'}],edges:[],has_more:true}
const flush = () => new Promise(resolve => setImmediate(resolve))

function setup() {
  let namespace = 'synthetic-A'
  const pending = []
  const useAuth = selector => selector({spaceId:namespace})
  useAuth.getState = () => ({spaceId:namespace})
  const moduleData = {exports:{}}
  vm.runInNewContext(source, {module:moduleData,exports:moduleData.exports,require(name) {
    if(name==='@tanstack/react-query') return {...rq,useQuery:options=>options}
    if(name==='../store/auth') return {useAuth}
    if(name==='../lib/api') return {api:{graph:params=>new Promise((resolve,reject)=>pending.push({params,resolve,reject}))}}
    throw new Error(name)
  }})
  const {useGraph,qk} = moduleData.exports
  const client=new rq.QueryClient({defaultOptions:{queries:{staleTime:Infinity,retry:false,gcTime:0}}})
  client.setQueryData(qk.graph(initial),full)
  const observer=new rq.QueryObserver(client,useGraph(initial,{keepPreviousData:true}))
  const unsubscribe=observer.subscribe(()=>{})
  return {observer,pending,change(params,space=namespace) {
    namespace=space
    observer.setOptions(useGraph(params,{keepPreviousData:true}))
    return observer.getCurrentResult()
  },close(){unsubscribe();observer.destroy();client.clear()}}
}

test('仅当前开关借用同一范围图谱，真实响应到达后退出过渡状态',async()=>{
  const s=setup()
  try {
    const r=s.change({...initial,live_only:true})
    assert.equal(r.data,full);assert.equal(r.isPlaceholderData,true);assert.equal(r.isLoading,false)
    assert.equal(s.pending[0].params.live_only,true)
    const current={nodes:[],edges:[],has_more:false}
    s.pending[0].resolve(current);await flush()
    assert.equal(s.observer.getCurrentResult().data,current)
    assert.equal(s.observer.getCurrentResult().isPlaceholderData,false)
  } finally {s.close()}
})

for(const [label,params,namespace] of [
  ['隐私',{...initial,include_sensitive:false}],
  ['搜索',{...initial,q:'new'}],
  ['空间',initial,'synthetic-B'],
]) test(`图谱${label}范围变化不保留旧图或截断信息`,()=>{
  const s=setup()
  try {
    const r=s.change(params,namespace)
    assert.equal(r.data,undefined);assert.equal(r.isPlaceholderData,false)
  } finally {s.close()}
})

test('隐私筛选失败与重试不恢复旧图，迟到响应不覆盖新范围',async()=>{
  const s=setup()
  try {
    s.change({...initial,live_only:true})
    s.change({...initial,include_sensitive:false})
    s.pending[1].reject(new Error('synthetic failure'));await flush()
    assert.equal(s.observer.getCurrentResult().isError,true)
    assert.equal(s.observer.getCurrentResult().data,undefined)
    const retry=s.observer.refetch()
    const safe={nodes:[],edges:[],has_more:false}
    s.pending[2].resolve(safe);await retry
    s.pending[0].resolve(full);await flush()
    assert.equal(s.observer.getCurrentResult().data,safe)
  } finally {s.close()}
})
