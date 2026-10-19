const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints:[path.resolve(__dirname,'../src/components/SessionPicker.tsx')],
  bundle:true,platform:'node',format:'cjs',write:false,jsx:'automatic',
  external:['react','react/jsx-runtime','lucide-react','../hooks/queries'],
}).outputFiles[0].text

function nodes(tree) {
  if (!tree || typeof tree!=='object') return []
  return [tree,...(Array.isArray(tree)?tree:[].concat(tree.props?.children??[])).flatMap(nodes)]
}

function setup(result={data:{sessions:[{id:'default'},{id:'synthetic-a'},{id:'synthetic-b'}]}}) {
  const states=[],changes=[]
  let cursor=0,value='default'
  const moduleData={exports:{}}
  vm.runInNewContext(source,{
    module:moduleData,exports:moduleData.exports,
    require(name){
      if(name==='react')return {...React,useId:()=> 'synthetic-picker',useRef:()=>({current:null}),useEffect(){},
        useState(initial){const index=cursor++;if(!(index in states))states[index]=initial;return [states[index],next=>{states[index]=next}]}}
      if(name==='../hooks/queries')return {useSessions:()=>result}
      return require(name)
    },
  })
  function render(){cursor=0;return moduleData.exports.SessionPicker({value,onChange:next=>{value=next;changes.push(next)}})}
  const trigger=()=>nodes(render()).find(node=>node.props?.role==='combobox')
  return {render,trigger,changes,
    input:()=>nodes(render()).find(node=>node.type==='input'),
    submitNew(){nodes(render()).find(node=>node.type==='form').props.onSubmit({preventDefault(){}})},
    option:id=>nodes(render()).find(node=>node.props?.role==='option' && node.key===id),
    key(key){trigger().props.onKeyDown({key,preventDefault(){},stopPropagation(){}})},
  }
}

test('已有会话可选择，default 不重复，打开菜单不会切换当前会话',()=>{
  const s=setup()
  s.trigger().props.onClick()
  assert.deepEqual(s.changes,[])
  assert.equal(nodes(s.render()).filter(node=>node.props?.role==='option' && node.key==='default').length,1)
  s.option('synthetic-a').props.onClick()
  assert.deepEqual(s.changes,['synthetic-a'])
  assert.equal(s.trigger().props['aria-expanded'],false)
})

test('键盘选择、取消及移出菜单不会触发意外切换',()=>{
  const s=setup()
  s.key('End');s.key('Enter')
  assert.deepEqual(s.changes,['synthetic-b'])
  s.key('Home');s.key('Escape')
  assert.deepEqual(s.changes,['synthetic-b'])
  s.key(' ');s.key('Tab')
  assert.equal(s.trigger().props['aria-expanded'],true)
  s.render().props.onBlur({currentTarget:{contains:()=>false},relatedTarget:null})
  assert.equal(s.trigger().props['aria-expanded'],false)
  assert.deepEqual(s.changes,['synthetic-b'])
})

test('空库或索引请求失败时仍提供 default',()=>{
  for(const result of [{data:{sessions:[]}},{isError:true}]){
    const s=setup(result)
    s.trigger().props.onClick()
    assert.ok(s.option('default'))
    s.option('default').props.onClick()
    assert.deepEqual(s.changes,['default'])
  }
})

test('新 ID 仅在确认时选择，修剪空白并关闭菜单，空白不提交',()=>{
  const s=setup()
  s.trigger().props.onClick()
  s.input().props.onChange({target:{value:'  synthetic-new  '}})
  assert.deepEqual(s.changes,[])
  s.submitNew()
  assert.deepEqual(s.changes,['synthetic-new'])
  assert.equal(s.trigger().props['aria-expanded'],false)
  s.trigger().props.onClick()
  assert.ok(s.option('synthetic-new'))
  s.input().props.onChange({target:{value:'   '}})
  s.submitNew()
  assert.deepEqual(s.changes,['synthetic-new'])
})

test('输入法组词期间不会提交新 ID，Tab 可进入输入框，失焦不提交草稿',()=>{
  const s=setup()
  s.trigger().props.onClick();s.key('Tab')
  assert.ok(s.input())
  s.input().props.onCompositionStart()
  s.input().props.onChange({target:{value:'演示会话'}})
  s.submitNew()
  assert.deepEqual(s.changes,[])
  s.input().props.onCompositionEnd()
  s.submitNew()
  assert.deepEqual(s.changes,['演示会话'])
  s.trigger().props.onClick()
  s.input().props.onChange({target:{value:'synthetic-unsaved'}})
  s.render().props.onBlur({currentTarget:{contains:()=>false},relatedTarget:null})
  assert.deepEqual(s.changes,['演示会话'])
})
