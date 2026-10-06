const API_KEY="ba0c2dad52b3ec";
const LBC_HOME="https://www.leboncoin.fr/";
const LBC_SEARCH="https://api.leboncoin.fr/finder/search";
let working=false;

function sleep(ms){return new Promise(r=>setTimeout(r,ms));}

async function waitTabLoaded(tabId,timeout=30000){
  const existing=await chrome.tabs.get(tabId);
  if(existing.status==="complete")return;
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{chrome.tabs.onUpdated.removeListener(listener);reject(new Error("Leboncoin n'a pas fini de charger."));},timeout);
    function listener(id,info){
      if(id===tabId&&info.status==="complete"){clearTimeout(timer);chrome.tabs.onUpdated.removeListener(listener);resolve();}
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function findOrCreateLbcTab(){
  const tabs=await chrome.tabs.query({url:["https://www.leboncoin.fr/*"]});
  if(tabs.length&&tabs[0].id!=null)return tabs[0].id;
  const tab=await chrome.tabs.create({url:LBC_HOME,active:false});
  await waitTabLoaded(tab.id);
  return tab.id;
}

async function fetchFinderInRealTab(tabId,payload){
  const result=await chrome.scripting.executeScript({
    target:{tabId,frameIds:[0]},
    world:"MAIN",
    func:async(args)=>{
      const sleep=ms=>new Promise(r=>setTimeout(r,ms));
      const norm=s=>String(s||"").normalize("NFKD").replace(/[\u0300-\u036f]/g,"").toLowerCase().replace(/[^a-z0-9]/g,"");
      const cleanNum=v=>{
        if(v==null)return null;
        const n=Number(String(v).replace(/[^0-9.-]/g,""));
        return Number.isFinite(n)?n:null;
      };
      const attr=(ad,key)=>{
        for(const a of (ad.attributes||[]))if(String(a.key||"").toLowerCase()===key)return a.value;
        return null;
      };
      const extractKm=ad=>{
        for(const key of ["mileage","mileage_km","kilometrage"]){
          const n=cleanNum(attr(ad,key));
          if(n!=null&&n>=0&&n<=300000)return n;
        }
        for(const a of (ad.attributes||[])){
          if(String(a.key||"").toLowerCase()!=="mileage")continue;
          for(const field of ["value_label","value","values_label","values"]){
            const vv=a[field];
            for(const item of (Array.isArray(vv)?vv:[vv])){
              const n=cleanNum(item);
              if(n!=null&&n>=0&&n<=300000)return n;
            }
          }
        }
        const m=(String(ad.subject||"")+" "+String(ad.body||"")).match(/\b(?:[1-9]\d{2}|\d{4,6})\s*km\b/i);
        return m?cleanNum(m[1]):null;
      };
      const extractYear=ad=>{
        for(const key of ["regdate","registration_year","year"]){
          const n=parseInt(String(attr(ad,key)||"").slice(0,4),10);
          if(n>=1900&&n<=2100)return n;
        }
        const m=(String(ad.subject||"")+" "+String(ad.body||"")).match(/\b(20\d{2})\b/);
        return m?Number(m[1]):null;
      };
      const results=[];
      const years=Array.isArray(args.years)?args.years:[];
      for(const year of years){
        const queryText=String(args.brand||"")+" "+String(args.model||"")+" "+String(year);
        const requestBody={
          filters:{category:{id:"4"},keywords:{text:queryText},enums:{ad_type:["offer"]}},
          limit:35,limit_alu:0,offset:0,disable_total:true,extend:true,listing_source:"direct-search"
        };
        let response=null,lastErr="";
        for(let attempt=0;attempt<2;attempt++){
          try{
            response=await fetch("https://api.leboncoin.fr/finder/search",{
              method:"POST",
              credentials:"include",
              headers:{
                "accept":"application/json,application/hal+json",
                "content-type":"application/json",
                "api_key":"ba0c2dad52b3ec"
              },
              body:JSON.stringify(requestBody)
            });
            if(response.ok)break;
            lastErr="HTTP "+response.status;
          }catch(e){lastErr=String(e&&e.message||e);}
          await sleep(1200);
        }
        if(!response||!response.ok)throw new Error(queryText+": "+lastErr);
        const data=await response.json();
        for(const ad of (data.ads||[])){
          const title=String(ad.subject||""),body=String(ad.body||""),identity=title+" "+body;
          if(args.model&& !norm(identity).includes(norm(args.model)))continue;
          const p=ad.price_cents!=null?Number(ad.price_cents)/100:cleanNum(ad.price);
          if(!(p>=10000&&p<=150000))continue;
          const km=extractKm(ad),y=extractYear(ad),url=String(ad.url||"").trim();
          if(!years.includes(y)||!url)continue;
          results.push({source_domain:"leboncoin.fr",title,url,snippet:body.slice(0,1000),price:Math.round(p),price_source:"lbc_finder_browser",km,year:y,query:"LBC_BROWSER_FINDER",direct_listing:true,detail_scraped:true});
        }
        await sleep(800);
      }
      const seen=new Set(),unique=[];
      for(const x of results){if(!seen.has(x.url)){seen.add(x.url);unique.push(x);}}
      return unique;
    },
    args:[payload]
  });
  return result&&result[0]&&result[0].result||[];
}

chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
  if(message?.type!=="MASTERS_LBC_REQUEST")return;
  if(working){sendResponse({error:"Une collecte Leboncoin est déjà en cours."});return;}
  working=true;
  (async()=>{
    try{
      const tabId=await findOrCreateLbcTab();
      await chrome.tabs.update(tabId,{url:LBC_HOME,active:false});
      await waitTabLoaded(tabId);
      await sleep(1200);
      const ads=await fetchFinderInRealTab(tabId,message.payload||{});
      sendResponse({ads});
    }catch(e){sendResponse({error:String(e&&e.message||e)});}
    finally{working=false;}
  })();
  return true;
});