const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('static/ml-national.js', 'utf8');
(async () => {
for (const mode of ['home', 'search']) for (const located of [false, true]) for (const all of [false, true]) {
  const events = {};
  const copy = {};
  const section = {dataset:{mode},hidden:true,querySelector:() => copy};
  let resolve;
  const elements = {'ml-national':section,'ml-national-products':{},'ml-national-status':{},'ml-national-priority':{after:() => {}},'search-inp':{value:'ricosol'}};
  vm.runInNewContext(script, {
    document:{getElementById:id=>elements[id],addEventListener:(key,fn)=>{events[key]=fn;}},
    localStorage:{getItem:()=>located?'-22.5':null},location:{search:all?'?nacionais=1':''},URLSearchParams,
    fetch:()=>new Promise(r=>{resolve=r;}),setTimeout,clearTimeout,window:{}
  });
  events['pq:regional-coverage']({detail:{integrated:true}});
  resolve({ok:true,json:async()=>({produtos:[{nome:'Ricosol',preco:39.99,url:'https://www.mercadolivre.com.br/teste'}]})});
  await new Promise(r=>setImmediate(r));
  assert.equal(section.hidden,true,'late catalog must not reveal covered region');
  events['pq:regional-coverage']({detail:{integrated:false}});
  assert.equal(section.hidden,false);
  assert.match(copy.textContent,/Ainda/);
  events['pq:regional-coverage']({detail:{pending:true}});
  assert.equal(section.hidden,true);
  events['pq:regional-coverage']({detail:{integrated:true}});
  assert.equal(section.hidden,true);
}
console.log('Coverage visibility passed for home, search, saved location and national page.');
})();
