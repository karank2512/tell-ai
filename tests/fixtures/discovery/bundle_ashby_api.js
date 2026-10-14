"use strict";(self.webpackChunk_N_E=self.webpackChunk_N_E||[]).push([[412],{7712:function(e,t,n){n.r(t);var r=n(5893),o=n(7294);
let a="https:\/\/api.ashbyhq.com\/posting-api\/job-board\/acme?includeCompensation=true";
function s(){let[e,t]=(0,o.useState)([]);return(0,o.useEffect)(()=>{fetch(a).then(e=>e.json()).then(e=>t(e.jobs))},[]),(0,r.jsx)("ul",{children:e.map(e=>(0,r.jsx)("li",{children:e.title},e.id))})}
t.default=s}}]);
