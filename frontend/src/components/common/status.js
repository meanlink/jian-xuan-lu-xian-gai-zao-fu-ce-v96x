import {JobStatus} from '../../constants/enums.js';
import {esc} from '../../utils/dom.js';
export const status=v=>'<span class="status '+esc(v)+'">'+esc(JobStatus[v]||v)+'</span>';
export function progressBar(p){
 if(!p||!p.total)return '<span class="progress-note">尚无分段</span>';
 return '<div class="progress" title="'+p.completed+'/'+p.total+' 段已完成"><div class="progress-track"><i style="width:'+Math.max(0,Math.min(100,p.percent))+'%"></i></div><span>'+p.completed+' / '+p.total+' 段 · '+p.percent+'%</span></div>';
}
