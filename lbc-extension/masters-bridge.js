window.addEventListener("message",(event)=>{
  if(event.source!==window||!event.data||event.data.type!=="MASTERS_LBC_REQUEST")return;
  chrome.runtime.sendMessage({type:"MASTERS_LBC_REQUEST",requestId:event.data.requestId,payload:event.data.payload})
    .then(result=>{
      window.postMessage({type:"MASTERS_LBC_RESULT",requestId:event.data.requestId,...(result||{error:"Réponse vide de l'extension."})},"*");
    })
    .catch(error=>{
      window.postMessage({type:"MASTERS_LBC_RESULT",requestId:event.data.requestId,error:String(error&&error.message||error)},"*");
    });
});