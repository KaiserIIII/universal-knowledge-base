import {t} from './i18n.js';

export function mergeFileOptions(existing,incoming,selectedIds){
  const rows=new Map([...existing,...incoming].map(row=>[row.id,row]));
  for(const id of selectedIds)if(!rows.has(id))rows.set(id,{id,filename:t('已选择的文件（列表外）')});
  const selected=new Set(selectedIds);
  return [...rows.values()].map(row=>({...row,selected:selected.has(row.id)}));
}
