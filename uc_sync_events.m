#import <Cocoa/Cocoa.h>
#import <Network/Network.h>

@interface SyncEvents : NSObject
@property NSTask *task;
@property NSTimer *timer;
@property NSMutableOrderedSet<NSString *> *reasons;
@property NSString *activeReason;
@property BOOL networkKnown;
@property BOOL networkOnline;
@property BOOL testing;
@property NSInteger completedExit;
- (void)queue:(NSString *)reason delay:(double)delay;
- (void)networkChanged:(BOOL)online;
- (void)fire;
- (void)saveStatus;
@end

@implementation SyncEvents
- (instancetype)init {
    self=[super init];
    if(self) { _reasons=[NSMutableOrderedSet orderedSet]; _completedExit=-1; }
    return self;
}
- (void)saveStatus {
    if(self.testing) return;
    NSDictionary *state=@{@"pid":@(NSProcessInfo.processInfo.processIdentifier),
        @"network_known":@(self.networkKnown), @"network_available":@(self.networkOnline),
        @"running":@(self.task.running), @"active_trigger":self.activeReason ?: @"",
        @"pending_triggers":self.reasons.array, @"last_exit_code":@(self.completedExit),
        @"next_run_at":self.timer.fireDate.description ?: @"", @"updated_at":NSDate.date.description};
    NSData *data=[NSJSONSerialization dataWithJSONObject:state options:NSJSONWritingPrettyPrinted error:nil];
    [data writeToFile:[NSHomeDirectory() stringByAppendingPathComponent:@".config/uc-sync/watcher-status.json"] atomically:YES];
}
- (void)queue:(NSString *)reason delay:(double)delay {
    BOOL scheduled=[reason isEqualToString:@"three-hour-check"] || [reason isEqualToString:@"retry-after-failure"];
    if(!scheduled) {
        [self.reasons removeObject:@"three-hour-check"];
        [self.reasons removeObject:@"retry-after-failure"];
    }
    [self.reasons addObject:reason];
    if(self.task.running) { [self saveStatus]; return; }
    // A burst of events cannot postpone an already imminent sync indefinitely.
    NSDate *wanted=[NSDate dateWithTimeIntervalSinceNow:delay];
    if(!self.timer.valid || [self.timer.fireDate compare:wanted]==NSOrderedDescending) {
        [self.timer invalidate];
        self.timer=[NSTimer scheduledTimerWithTimeInterval:delay repeats:NO block:^(NSTimer *timer){[self fire];}];
    }
    [self saveStatus];
}
- (void)networkChanged:(BOOL)online {
    BOOL restored=self.networkKnown && !self.networkOnline && online;
    self.networkKnown=YES;
    self.networkOnline=online;
    if(restored) [self queue:@"network-restored" delay:10];
    [self saveStatus];
}
- (void)fire {
    if(self.task.running || self.testing) return;
    [self.timer invalidate];self.timer=nil;
    self.activeReason=[self.reasons.array componentsJoinedByString:@", "];
    [self.reasons removeAllObjects];
    NSTask *task=[NSTask new];self.task=task;
    task.executableURL=[NSURL fileURLWithPath:@"/usr/bin/python3"];
    task.arguments=@[@"-B",[NSHomeDirectory() stringByAppendingPathComponent:@".config/uc-sync/uc_sync_and_mirror.py"]];
    NSMutableDictionary *env=[NSProcessInfo.processInfo.environment mutableCopy];
    env[@"UC_SYNC_TRIGGER"]=self.activeReason ?: @"timer";
    if(self.networkKnown) env[@"UC_NETWORK_AVAILABLE"]=self.networkOnline ? @"1" : @"0";
    task.environment=env;
    task.standardOutput=[NSFileHandle fileHandleWithNullDevice];task.standardError=[NSFileHandle fileHandleWithNullDevice];
    task.terminationHandler=^(NSTask *finished){dispatch_async(dispatch_get_main_queue(),^{
        self.task=nil;self.completedExit=finished.terminationStatus;
        if(self.reasons.count) [self queue:@"queued-event" delay:5];
        else [self queue:finished.terminationStatus==0 ? @"three-hour-check" : @"retry-after-failure"
                     delay:finished.terminationStatus==0 ? 10800 : 300];
    });};
    NSError *error=nil;
    if(![task launchAndReturnError:&error]) { self.task=nil;[self queue:@"launch-retry" delay:300]; }
    [self saveStatus];
}
@end

int main(int argc,const char *argv[]) { @autoreleasepool {
    SyncEvents *events=[SyncEvents new];
    events.testing=argc>1 && strcmp(argv[1],"--test-events")==0;
    NSNotificationCenter *nc=NSWorkspace.sharedWorkspace.notificationCenter;
    [nc addObserverForName:NSWorkspaceDidWakeNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n){[events queue:@"wake" delay:15];}];
    [nc addObserverForName:NSWorkspaceDidMountNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n){
        NSURL *url=n.userInfo[NSWorkspaceVolumeURLKey];NSString *uuid=nil;
        [url getResourceValue:&uuid forKey:NSURLVolumeUUIDStringKey error:nil];
        NSData *data=[NSData dataWithContentsOfFile:[NSHomeDirectory() stringByAppendingPathComponent:@".config/uc-sync/destinations.json"]];
        NSDictionary *config=data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if(uuid && [uuid caseInsensitiveCompare:config[@"ssd_volume_uuid"] ?: @""]==NSOrderedSame)
            [events queue:@"extreme-mounted" delay:5];
    }];
    [nc addObserverForName:NSWorkspaceDidLaunchApplicationNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n){
        NSRunningApplication *app=n.userInfo[NSWorkspaceApplicationKey];
        if([app.bundleIdentifier isEqualToString:@"com.google.drivefs"]) [events queue:@"drive-started" delay:30];
    }];
    if(events.testing) {
        [events networkChanged:YES];
        if(events.reasons.count) return 2;
        [events networkChanged:NO];[events networkChanged:YES];
        if(![events.reasons containsObject:@"network-restored"]) return 3;
        NSTimer *before=events.timer;[events networkChanged:YES];
        if(events.timer!=before || events.reasons.count!=1) return 4;
        [nc postNotificationName:NSWorkspaceDidWakeNotification object:NSWorkspace.sharedWorkspace];
        if(![events.reasons containsObject:@"wake"]) return 5;
        NSUInteger count=events.reasons.count;
        [nc postNotificationName:NSWorkspaceDidMountNotification object:NSWorkspace.sharedWorkspace userInfo:@{NSWorkspaceVolumeURLKey:[NSURL fileURLWithPath:@"/"]}];
        if(events.reasons.count!=count) return 7;
        puts("PASS: offline-to-online trigger, duplicate suppression, event coalescing, wake, unrelated mount filtering. Synthetic notifications only.");
        return 0;
    }
    nw_path_monitor_t monitor=nw_path_monitor_create();
    nw_path_monitor_set_queue(monitor,dispatch_get_main_queue());
    nw_path_monitor_set_update_handler(monitor,^(nw_path_t path){
        [events networkChanged:nw_path_get_status(path)==nw_path_status_satisfied];
    });
    nw_path_monitor_start(monitor);
    [events queue:@"login-or-service-start" delay:10];
    [NSRunLoop.mainRunLoop run];
    nw_path_monitor_cancel(monitor);
} return 0; }
