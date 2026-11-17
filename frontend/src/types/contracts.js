/** @typedef {{total:number,completed:number,running:number,pending:number,percent:number,paused:boolean}} Progress */
/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean,progress:Progress,waiting_reason:string}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
export {};
