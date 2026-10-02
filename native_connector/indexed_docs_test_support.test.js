'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {bodyForText,textOf,applyDocumentBatch,scalarBoundary}=require('./indexed_docs_test_support');
function resource(text='first\nlong 😀 line with $& literal\nlast\n'){
  return {documentId:'doc',revisionId:'r1',tabs:[{tabProperties:{tabId:'tab'},
    documentTab:{body:bodyForText(text,{runSize:7})}}]};
}
function request(document,text='replacement 😀 $&\nlast'){
  return {document_id:'doc',write_control:{requiredRevisionId:'r1'},requests:[
    {deleteContentRange:{range:{startIndex:1,endIndex:textOf(document,'tab').length,tabId:'tab'}}},
    {insertText:{location:{index:1,tabId:'tab'},text}}]};
}
test('Synthetic Docs resources expose contiguous UTF-16 paragraph/run indices without splitting emoji',()=>{
  const text='begin\n'+'x'.repeat(25000)+'😀😀最後\nend\n',document=resource(text);
  let next=1,paragraphs=0,runs=0;
  for(const element of document.tabs[0].documentTab.body.content.slice(1)){
    assert.equal(element.startIndex,next);paragraphs++;let paragraph='';
    for(const run of element.paragraph.elements){
      assert.equal(run.startIndex,next);assert.equal(run.endIndex-run.startIndex,run.textRun.content.length);
      assert(scalarBoundary(text,run.startIndex-1));assert(scalarBoundary(text,run.endIndex-1));
      next=run.endIndex;paragraph+=run.textRun.content;runs++;
    }
    assert(paragraph.endsWith('\n'));assert.equal(paragraph.indexOf('\n'),paragraph.length-1);
    assert.equal(element.endIndex,next);
  }
  assert.equal(next,1+text.length);assert.equal(paragraphs,3);assert(runs>3000);
  assert.equal(textOf(document,'tab'),text);
});
test('Indexed deletion/insertion commits one revision with two empty replies and preserves final newline',()=>{
  const document=resource(),before=structuredClone(document),args=request(document);
  const applied=applyDocumentBatch(document,args,{tabId:'tab',allowLegacy:false,runSize:2});
  assert.equal(textOf(applied.document,'tab'),'replacement 😀 $&\nlast\n');
  assert.deepEqual(applied.response,{documentId:'doc',replies:[{},{}],writeControl:{requiredRevisionId:'r1-next'}});
  assert.deepEqual(document,before);assert.equal(applied.document.revisionId,'r1-next');
});
for(const [label,mutate] of [
  ['empty batch',a=>{a.requests=[];}],
  ['ignored trailing request',a=>{a.requests.push({unknownRequest:{}});}],
  ['only delete',a=>{a.requests.pop();}],
  ['unknown second request',a=>{a.requests[1]={unsupported:{}};}],
  ['wrong revision',a=>{a.write_control.requiredRevisionId='other';}],
  ['target revision substitute',a=>{a.write_control={targetRevisionId:'r1'};}],
  ['wrong document',a=>{a.document_id='other';}],
  ['wrong delete tab',a=>{a.requests[0].deleteContentRange.range.tabId='other';}],
  ['wrong insert tab',a=>{a.requests[1].insertText.location.tabId='other';}],
  ['partial deletion',a=>{a.requests[0].deleteContentRange.range.endIndex--;}],
  ['delete final newline',a=>{a.requests[0].deleteContentRange.range.endIndex++;}],
  ['zero start',a=>{a.requests[0].deleteContentRange.range.startIndex=0;}],
  ['string range index',a=>{a.requests[0].deleteContentRange.range.startIndex='1';}],
  ['boolean range index',a=>{a.requests[0].deleteContentRange.range.startIndex=true;}],
  ['noninteger range index',a=>{a.requests[0].deleteContentRange.range.endIndex=3.5;}],
  ['wrong insertion index',a=>{a.requests[1].insertText.location.index=2;}],
  ['unsupported insertion selector',a=>{a.requests[1].insertText.endOfSegmentLocation={};}],
  ['nonstring insertion',a=>{a.requests[1].insertText.text={};}],
  ['extra deletion property',a=>{a.requests[0].deleteContentRange.extra={};}],
])test('Malformed synthetic batch cannot partially delete: '+label,()=>{
  const document=resource(),before=structuredClone(document),args=request(document);mutate(args);
  assert.throws(()=>applyDocumentBatch(document,args,{tabId:'tab'}));assert.deepEqual(document,before);
});
test('Legacy child replaceAllText remains literal and isolated from Global indexed mode',()=>{
  const document=resource('child old\n'),args={document_id:'doc',write_control:{requiredRevisionId:'r1'},requests:[
    {replaceAllText:{containsText:{text:'old',matchCase:true,searchByRegex:false},replaceText:'$& 😀',tabsCriteria:{tabIds:['tab']}}}]};
  const result=applyDocumentBatch(document,args,{tabId:'tab'});
  assert.equal(textOf(result.document,'tab'),'child $& 😀\n');
  assert.deepEqual(result.response.replies,[{replaceAllText:{occurrencesChanged:1}}]);
  assert.throws(()=>applyDocumentBatch(document,args,{tabId:'tab',allowLegacy:false}));
});
