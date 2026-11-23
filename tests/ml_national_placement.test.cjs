const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('static/ml-national.js', 'utf8');

for (const mode of ['home', 'search']) {
  for (const located of [false, true]) {
    let position;
    const events = {};
    const section = {dataset:{mode}, hidden:true};
    const elements = {
      'ml-national':section,
      'ml-national-products':{},
      'ml-national-status':{},
      'ml-national-priority':{after:node => {assert.equal(node,section);position='priority';}},
      'ml-national-secondary':{after:node => {assert.equal(node,section);position='secondary';}},
      'search-inp':{value:'ricosol'},
    };
    vm.runInNewContext(script, {
      document:{getElementById:id => elements[id],addEventListener:(key,fn) => {events[key]=fn;}},
      localStorage:{getItem:() => located ? '-22.5' : null},
      location:{search:''}, URLSearchParams,
      // A pending fetch keeps this test focused on geographic placement, independent of stock.
      fetch:() => new Promise(() => {}), setTimeout, clearTimeout,
    });
    assert.equal(position,located ? 'secondary' : 'priority');
    events['pq:regional-coverage']({detail:{pending:true}});
    assert.equal(position,'secondary');
    events['pq:regional-coverage']({detail:{integrated:true}});
    assert.equal(position,'secondary');
    events['pq:regional-coverage']({detail:{integrated:false}});
    assert.equal(position,'priority');
    events['pq:regional-coverage']({detail:{integrated:true}});
    assert.equal(position,'secondary');
  }
}
console.log('Home/search: local priority, uncovered regions, and location changes passed.');
