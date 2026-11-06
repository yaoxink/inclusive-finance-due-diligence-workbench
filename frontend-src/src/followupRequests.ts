export type FollowupRequestKind='background'|'manual';
export type FollowupRequest={id:number;caseId:string;kind:FollowupRequestKind;controller:AbortController};

/** Only the current request owns state updates and completion. Human work wins polls. */
export class FollowupRequests{
  private sequence=0;
  private active:FollowupRequest|null=null;

  begin(caseId:string,kind:FollowupRequestKind):FollowupRequest|null{
    if(this.active&&(kind==='background'||this.active.kind==='manual'))return null;
    this.cancel();
    const request={id:++this.sequence,caseId,kind,controller:new AbortController()};
    this.active=request;
    return request;
  }

  owns(request:FollowupRequest):boolean{
    return this.active===request&&!request.controller.signal.aborted;
  }

  finish(request:FollowupRequest):boolean{
    if(!this.owns(request))return false;
    this.active=null;
    return true;
  }

  hasManual():boolean{return this.active?.kind==='manual';}

  cancelBackground():void{
    if(this.active?.kind==='background')this.cancel();
  }

  cancel():void{
    const previous=this.active;
    // Invalidate first: even synchronous abort listeners must not own this request.
    this.active=null;
    previous?.controller.abort();
  }
}
