(()=>{const $=s=>document.querySelector(s),PAL=['#1f77b4','#d62728','#2ca02c','#ff7f0e','#9467bd','#8c564b'],GREY='#b9c0cc',D={},CH={},P=new URLSearchParams(location.search);
let sel=[],season;const G=t=>t.games||1,rt=(a,b)=>a/(b||1),pg=document.body.dataset.page;
const LG=t=>t.line_games>0,sm=(a,i,j)=>a.slice(i,j).reduce((x,y)=>x+y,0),rec=(w,l)=>`${w}-${l}`,sc=t=>((D.scores&&D.scores.seasons[season])||{})[t.team_id];
const CATS={
strength:{page:'strength.html',title:'Team strength & luck',blurb:'Who is really good, and who is only lucky.',unit:'Pennant Score (0 to 100)',f:t=>(sc(t)||{}).score,d:0,best:1,cta:'Open the full strength & luck dashboard'},
offense:{page:'offense.html',title:'Offense',blurb:'How well each team scores.',unit:'Runs per game',f:t=>t.runs_scored/G(t),d:2,best:1,
 cols:[['Runs/G',t=>t.runs_scored/G(t),2,1],['Hits/G',t=>t.hits_for/G(t),2,1],['Runs per hit',t=>rt(t.runs_scored,t.hits_for),3,1],['Times shut out',t=>t.shutouts_against,0,-1],['Blowout wins (5+ runs)',t=>t.blowout_wins,0,1],['Scoring innings %',t=>LG(t)?100*t.scoring_innings/t.innings_batted:null,1,1],['Big innings (3+ runs)',t=>LG(t)?t.big_innings:null,0,1],['Runs in 7th+ (% of runs)',t=>LG(t)?100*sm(t.runs_by_inning,6,10)/Math.max(1,sm(t.runs_by_inning,0,10)):null,1,0]],
 note:'The free data has team totals and inning-by-inning scores, with no batting average, walks, or exit velocity. Runs per hit is a rough sign of how efficiently hits turn into runs. Big innings are innings with three or more runs.'},
pitching:{page:'pitching.html',title:'Pitching & defense',blurb:'How well each team prevents runs: pitching and fielding together.',unit:'Runs allowed per game',f:t=>t.runs_allowed/G(t),d:2,best:-1,
 cols:[['Runs allowed/G',t=>t.runs_allowed/G(t),2,-1],['Hits allowed/G',t=>t.hits_against/G(t),2,-1],['Runs per hit allowed',t=>rt(t.runs_allowed,t.hits_against),3,-1],['Shutouts pitched',t=>t.shutouts_pitched,0,1],['Blowout losses (5+ runs)',t=>t.blowout_losses,0,-1],['Errors/G',t=>t.errors/G(t),3,-1],['Runs allowed in 7th+ /G',t=>LG(t)?sm(t.runs_allowed_by_inning,6,10)/t.line_games:null,2,-1]],
 note:'Runs allowed combine pitching and defense, and the free data cannot separate them, starters from relievers, or strikeouts from balls in play. Errors are a weak, old-fashioned fielding measure: they miss range and positioning, and real defensive metrics need tracking data that is not free.'},
gameflow:{page:'gameflow.html',title:'Game flow',blurb:'How games are won and lost: comebacks, walk-offs, and blown leads.',unit:'Comeback wins',f:t=>LG(t)?t.comeback_wins:null,d:0,best:1,
 cols:[['Comeback wins',t=>LG(t)?t.comeback_wins:null,0,1],['Lead after 6: W-L',t=>LG(t)?100*t.led_after_six_wins/(t.led_after_six||1):null,0,1,t=>LG(t)?rec(t.led_after_six_wins,t.blown_leads):'–'],['Blown leads',t=>LG(t)?t.blown_leads:null,0,-1],
 ['Walk-off W-L',t=>LG(t)?t.walkoff_wins-t.walkoff_losses:null,0,1,t=>LG(t)?rec(t.walkoff_wins,t.walkoff_losses):'–'],['Heartbreak losses',t=>LG(t)?t.heartbreak_losses:null,0,-1],
 ['Blowout games (5+ runs)',t=>t.blowout_wins+t.blowout_losses,0,0],['One-run games',t=>t.one_run_wins+t.one_run_losses,0,0],['Extra-inning games',t=>t.extra_wins+t.extra_losses,0,0]],
 note:'Built from inning-by-inning scores. A comeback win is a win after trailing through six innings, a blown lead is a loss after leading through six, and a heartbreak loss is a blown lead or a walk-off loss. This is a team-level view: it cannot say who gave up the lead.'},
close:{page:'closegames.html',title:'Close games',blurb:'How teams fare when games are tight.',unit:'Win % in one-run games',f:t=>100*rt(t.one_run_wins,t.one_run_wins+t.one_run_losses),d:0,best:1,
 cols:[['One-run W-L',t=>t.one_run_wins-t.one_run_losses,0,1,t=>rec(t.one_run_wins,t.one_run_losses)],['One-run win %',t=>100*rt(t.one_run_wins,t.one_run_wins+t.one_run_losses),0,1],['Close-game luck (z)',t=>t.one_run_z,2,0],['Extra-inning W-L',t=>t.extra_wins-t.extra_losses,0,1,t=>rec(t.extra_wins,t.extra_losses)],['Luck (wins)',t=>t.luck,1,0]],
 note:'True clutch hitting needs play-by-play data we do not have for free. Close-game records are mostly luck and rarely repeat, so a z above +1.5 or below -1.5 is a flag, not a talent.'}};
const rows=()=>D.standings.seasons[season].teams,qs=()=>{const p=new URLSearchParams();if(sel.length)p.set('teams',sel.join(','));p.set('season',season);return '?'+p},
 num=(v,d)=>v==null||isNaN(v)?'–':v.toFixed(d),col=a=>PAL[sel.indexOf(a)%PAL.length];
function ranked(c,k=c.f){return rows().filter(t=>t.games&&k(t)!=null).sort((a,b)=>(k(b)-k(a))*(c.best===-1?-1:1))}
function bars(id,c,list){CH[id]&&CH[id].destroy();CH[id]=new Chart($('#'+id),{type:'bar',data:{labels:list.map(t=>t.abbreviation),datasets:[{data:list.map(t=>+c.f(t).toFixed(c.d+1)),backgroundColor:list.map(t=>sel.includes(t.abbreviation)?col(t.abbreviation):GREY)}]},
 options:{indexAxis:'y',maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{title:{display:true,text:c.unit}}}}})}
function head(){document.title=(pg==='home'?'':(CATS[pg]||{}).title+' · ')+'Pennant Math';const N=[['index.html','Home','home']].concat(Object.entries(CATS).map(([k,c])=>[c.page,c.title.split(' ')[0]==='Team'?'Strength & luck':c.title,k]),[['methods.html','How it works','x']]);
 $('#hd').innerHTML=`<h1>Pennant Math</h1><nav>${N.map(([h,l,k])=>`<a href="${h}${qs()}" class="${k===pg?'on':''}">${l}</a>`).join('')}</nav>`}
function seasonPick(){const all=Object.keys(D.standings.seasons).sort(),ok=all.filter(k=>D.standings.seasons[k].as_of),ss=ok.length?ok:all;season=ss.includes(P.get('season'))?P.get('season'):ss[ss.length-1];
 return {ss,html:`<label>Season <select id="sn">${ss.map(s=>`<option ${s===season?'selected':''}>${s}</option>`).join('')}</select></label>`}}
function setSel(a){sel=a;try{localStorage.setItem('pm_teams',sel.join(','))}catch(e){}history.replaceState(null,'','?'+qs().slice(1));head();if(pg==='home'){picker();strip();home()}}
function picker(){const box=$('#pick');if(!box)return;const g={};rows().forEach(t=>(g[`${t.league==='American'?'AL':'NL'} ${t.division}`]??=[]).push(t));
 box.innerHTML=Object.keys(g).sort().map(k=>`<h4>${k}</h4>`+g[k].map(t=>`<label><input type="checkbox" value="${t.abbreviation}" ${sel.includes(t.abbreviation)?'checked':''}> ${t.name}</label>`).join('')).join('');
 box.onchange=()=>{const now=[...box.querySelectorAll('input:checked')].map(i=>i.value);setSel([...sel.filter(a=>now.includes(a)),...now.filter(a=>!sel.includes(a))])};$('#clear').onclick=()=>setSel([])}
function strip(){const m=sel.map(a=>rows().find(t=>t.abbreviation===a)).filter(Boolean);$('#mine').hidden=!m.length;$('#pickSum').textContent=sel.length?sel.join(', '):'Pick your teams';
 $('#mine').innerHTML=m.length?`<h2>Your teams</h2><div class="chips">${m.map(t=>`<span class="chip" style="border-color:${col(t.abbreviation)}"><b>${t.name}</b> ${rec(t.wins,t.losses)} · Pennant Score ${num((sc(t)||{}).score,0)} · Luck ${num(t.luck,1)}</span>`).join('')}</div>`:''}
function home(){const a=D.standings.seasons[season];$('#asof').textContent=`Season ${season} · through ${a.as_of||'n/a'}${a.complete?' (final)':''}`;
 $('#mods').innerHTML=Object.entries(CATS).map(([k,c])=>`<article class="mod"><h3>${c.title}</h3><p class="blurb">${c.blurb}</p><p class="lead" id="l_${k}"></p><div class="cv"><canvas id="c_${k}"></canvas></div><a class="cta" href="${c.page}${qs()}">${c.cta||'See all '+c.title.toLowerCase()+' stats'} →</a></article>`).join('');
 Object.entries(CATS).forEach(([k,c])=>{const L=ranked(c).slice(0,5);if(!L.length)return;const top=L[0];$('#l_'+k).textContent=c.best===0?`Most lopsided: ${top.abbreviation}, ${num(c.f(top),c.d)}%`:`${c.best===-1?'Best':'Leader'}: ${top.abbreviation}, ${num(c.f(top),c.d)} (${c.unit.toLowerCase()})`;bars('c_'+k,c,L)})}
function cat(){const c=CATS[pg],a=D.standings.seasons[season];$('#asof').textContent=`Season ${season} · through ${a.as_of||'n/a'}${a.complete?' (final)':''}`;
 $('#ttl').textContent=c.title;$('#blurb').textContent=c.blurb;$('#note').textContent=c.note;$('#cn').textContent=c.unit;let sk=-1,dir=c.best===-1?1:-1;
 const draw=()=>{const L=ranked(c);bars('chart',c,L);const C=c.cols,v=t=>(sk<0?c.f(t):C[sk][1](t))??-1e9;
  const R=[...rows().filter(t=>t.games)].sort((x,y)=>(v(x)-v(y))*dir);
  $('#tbl').innerHTML=`<tr><th data-i="-1">Team</th>${C.map((x,i)=>`<th data-i="${i}">${x[0]}${i===sk?(dir>0?' ▲':' ▼'):''}</th>`).join('')}</tr>`+R.map(t=>`<tr class="${sel.includes(t.abbreviation)?'sel':''}"><td>${t.name}</td>${C.map(x=>`<td>${x[4]?x[4](t):num(x[1](t),x[2])}</td>`).join('')}</tr>`).join('');
  document.querySelectorAll('#tbl th').forEach(h=>h.onclick=()=>{const i=+h.dataset.i;if(i<0){sk=-1;dir=c.best===-1?1:-1;draw();return}dir=i===sk?-dir:(C[i][3]===-1?1:-1);sk=i;draw()})};draw()}
async function boot(){for(const n of ['standings','scores']){try{const r=await fetch(`data/${n}.json`);D[n]=r.ok?await r.json():null}catch(e){D[n]=null}}
 if(!D.standings){$('#msg').textContent='Data files not found. Run the GitHub workflow, then reload.';return}
 let t=P.get('teams');if(t==null){try{t=localStorage.getItem('pm_teams')||''}catch(e){t=''}}
 const sp=seasonPick(),valid=new Set(Object.values(D.standings.seasons).flatMap(s=>s.teams.map(x=>x.abbreviation)));sel=t.split(',').filter(a=>valid.has(a));
 $('#sp').innerHTML=sp.html;$('#sn').onchange=e=>{location.search='?'+new URLSearchParams({...(sel.length?{teams:sel.join(',')}:{}),season:e.target.value})};
 head();pg==='home'?(picker(),strip(),home()):cat();if($('#picked'))$('#picked').textContent=sel.length?`Highlighting: ${sel.join(', ')}. Change your teams on the Strength & luck page.`:'No teams picked yet: choose some on the Strength & luck page and they will be highlighted here.'}
boot()})();
