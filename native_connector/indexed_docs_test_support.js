'use strict';
// Strict, synthetic Docs provider for offline Global integration tests. Indexes
// use JavaScript's UTF-16 units, as Docs does; no network or provider state.
const assert=require('node:assert/strict');

function scalarBoundary(text,index){
  if(index<=0||index>=text.length)return true;
  const before=text.charCodeAt(index-1),after=text.charCodeAt(index);
  return !(before>=0xd800&&before<=0xdbff&&after>=0xdc00&&after<=0xdfff);
}
function bodyForText(text,{runSize=1024}={}){
  assert.equal(typeof text,'string');assert(text.endsWith('\n'));
  assert(Number.isInteger(runSize)&&runSize>0);
  const content=[{startIndex:0,endIndex:1,sectionBreak:{}}];let index=1;
  for(const line of text.match(/[^\n]*\n/g)||[]){
    const startIndex=index,elements=[];let offset=0;
    while(offset<line.length){
      let end=Math.min(line.length,offset+runSize);
      if(!scalarBoundary(line,end))end++;
      const part=line.slice(offset,end);
      elements.push({startIndex:index,endIndex:index+part.length,textRun:{content:part}});
      index+=part.length;offset=end;
    }
    content.push({startIndex,endIndex:index,paragraph:{elements}});
  }
  return {content};
}
function selectedTab(document,tabId){
  const found=[];
  function visit(tabs){for(const tab of tabs||[]){
    if((tab.tabProperties?.tabId??tab.tabId)===tabId)found.push(tab);
    visit(tab.childTabs);
  }}
  visit(document.tabs);assert.equal(found.length,1,'exact synthetic tab required');return found[0];
}
function textOf(document,tabId){
  const tab=selectedTab(document,tabId),body=tab.documentTab?.body??tab.body;
  assert(Array.isArray(body?.content));let text='';
  for(const element of body.content){
    if(element.sectionBreak)continue;
    assert(element.paragraph,'unsupported synthetic structural element');
    for(const run of element.paragraph.elements){assert(run.textRun,'unsupported synthetic paragraph element');text+=run.textRun.content;}
  }
  return text;
}
function withText(document,tabId,text,options){
  const copy=structuredClone(document),tab=selectedTab(copy,tabId);
  if(tab.documentTab)tab.documentTab.body=bodyForText(text,options);else tab.body=bodyForText(text,options);
  return copy;
}
function reflowDocument(document,tabId,options){return withText(document,tabId,textOf(document,tabId),options);}
function exactKeys(value,keys){assert(value&&typeof value==='object'&&!Array.isArray(value));assert.deepEqual(Object.keys(value).sort(),[...keys].sort());}
function applyDocumentBatch(document,args,{tabId,runSize=1024,allowLegacy=true}={}){
  // Validate the entire batch before constructing the replacement resource.
  // A malformed insertion can never leave a partially deleted document behind.
  exactKeys(args,['document_id','requests','write_control']);
  assert.equal(args.document_id,document.documentId);
  assert.deepEqual(args.write_control,{requiredRevisionId:document.revisionId});
  assert(Array.isArray(args.requests));
  const current=textOf(document,tabId);let next,replies;
  if(args.requests.length===2){
    const [deletion,insertion]=args.requests;
    exactKeys(deletion,['deleteContentRange']);exactKeys(deletion.deleteContentRange,['range']);
    const range=deletion.deleteContentRange.range;exactKeys(range,['startIndex','endIndex','tabId']);
    assert.equal(range.tabId,tabId);assert(Number.isInteger(range.startIndex)&&Number.isInteger(range.endIndex));
    assert.equal(range.startIndex,1);assert.equal(range.endIndex,current.length);
    assert(current.endsWith('\n'),'provider final newline must survive');
    assert(scalarBoundary(current,range.endIndex-1));
    exactKeys(insertion,['insertText']);exactKeys(insertion.insertText,['location','text']);
    const insert=insertion.insertText;exactKeys(insert.location,['index','tabId']);
    assert.equal(insert.location.tabId,tabId);assert.equal(insert.location.index,range.startIndex);
    assert.equal(typeof insert.text,'string');assert(insert.text.length>0);
    next=current.slice(0,range.startIndex-1)+insert.text+current.slice(range.endIndex-1);
    replies=[{},{}];
  }else{
    assert.equal(args.requests.length,1);assert(allowLegacy,'Global queue updates must use indexed requests');
    const request=args.requests[0];exactKeys(request,['replaceAllText']);
    const replacement=request.replaceAllText;exactKeys(replacement,['containsText','replaceText','tabsCriteria']);
    exactKeys(replacement.containsText,['text','matchCase','searchByRegex']);
    assert.equal(replacement.containsText.matchCase,true);assert.equal(replacement.containsText.searchByRegex,false);
    assert.deepEqual(replacement.tabsCriteria,{tabIds:[tabId]});
    const old=replacement.containsText.text;assert.equal(typeof old,'string');assert(old.length>0);
    assert.equal(typeof replacement.replaceText,'string');
    const pieces=current.split(old),occurrences=pieces.length-1;
    // Join is deliberately literal: JS String.replace treats "$&" as syntax.
    next=pieces.join(replacement.replaceText);replies=[{replaceAllText:{occurrencesChanged:occurrences}}];
  }
  const updated=withText(document,tabId,next,{runSize});
  updated.revisionId=document.revisionId+'-next';
  return {document:updated,response:{documentId:document.documentId,replies,
    writeControl:{requiredRevisionId:updated.revisionId}}};
}
module.exports={bodyForText,textOf,reflowDocument,applyDocumentBatch,scalarBoundary};
