import 'dart:async';
import 'dart:html';
import 'dart:convert';

import 'package:angular/angular.dart';
import 'package:quickpin/authentication.dart';
import 'package:quickpin/query_watcher.dart';
import 'package:quickpin/component/breadcrumbs.dart';
import 'package:quickpin/component/pager.dart';
import 'package:quickpin/component/title.dart';
import 'package:quickpin/model/post.dart';
import 'package:quickpin/rest_api.dart';
import 'package:quickpin/sse.dart';

/// A component for posts by a specified profile.
@Component(
    selector: 'profile-posts',
    templateUrl: 'packages/quickpin/component/profile/posts.html',
    useShadowDom: false
)
class ProfilePostsComponent extends Object
                            implements ScopeAware {
    AuthenticationController auth;
    List<Breadcrumb> crumbs;

    String error = '';
    String id;
    int loading = 0;
    Pager pager;
    List<Post> posts;
    Scope scope;
    String siteName;
    String username;
    Map<String, Map> _runningJobs;
    bool failedTasks = false;
    List<Map> workers;
    List<Map> profilePostsWorkers;
    QueryWatcher _queryWatcher;

    final RestApiController api;

    final RouteProvider _rp;
    final TitleService _ts;
    final SseController _sse;

    /// Constructor.
    ProfilePostsComponent(this.api, this.auth, this._rp, this._sse, this._ts) {
        this.id = this._rp.parameters['id'];
        this._ts.title = 'Posts by ${id}';
        this._updateCrumbs();

        RouteHandle rh = this._rp.route.newHandle();

        this._queryWatcher = new QueryWatcher(
            rh,
            ['page', 'rpp'],
            this._fetchCurrentPage
        );

        // Add event listeners...
        List<StreamSubscription> listeners = [
            this._sse.onProfilePosts.listen((_) => this._fetchCurrentPage()),
            this._sse.onWorker.listen(this._workerListener),
        ];

        // ...and remove event listeners when we leave this route.
        UnsubOnRouteLeave(rh, [
            this._sse.onProfilePosts.listen(this._fetchCurrentPage),
            this._sse.onWorker.listen(this._workerListener),
        ]);

        this._fetchCurrentPage()
            .then((_) => this._fetchProfilePostsWorkers())
            .then((_) => this._fetchFailedProfilePostsTasks());
    }

    /// Listen for updates from background workers.
    void _workerListener(Event e) {
        Map json = JSON.decode(e.data);
        String status = json['status'];

        if (status == 'queued' || status == 'started' || status == 'finished') {
            // This information can only be fetched via REST.
            this._fetchProfilePostsWorkers();
        } else if (status == 'progress') {
            Map job = this._runningJobs[json['id']];

            if (job != null) {
                // Event contains all the data we need: no need for REST call.
                job['current'] = json['current'];
                job['progress'] = json['progress'];
            } else {
                // This is a job we don't know about: needs REST call.
                this._fetchProfilePostsWorkers();
            }
        } else if (status == 'failed') {
            this._fetchFailedProfilePostsTasks().then((_) => this._fetchProfilePostsWorkers());
        }
    }

    /// Fetch list of posts.
    Future _fetchCurrentPage() {
        Completer completer = new Completer();
        this.error = '';
        this.loading++;
        String pageUrl = '/api/profile/${this.id}/posts';
        Map urlArgs = {
            'page': this._queryWatcher['page'] ?? '1',
            'rpp': this._queryWatcher['rpp'] ?? '10',
        };

        this.api
            .get(pageUrl, urlArgs: urlArgs, needsAuth: true)
            .then((response) {
                this.siteName = response.data['site_name'];
                this.username = response.data['username'];
                this.posts = new List<Post>();

                response.data['posts'].forEach((post) {
                    this.posts.add(new Post.fromJson(post));
                });

                this.pager = new Pager(response.data['total_count'],
                                       int.parse(this._queryWatcher['page'] ?? '1'),
                                       resultsPerPage: int.parse(this._queryWatcher['rpp'] ?? '10'));

                this._ts.title = 'Posts by ${this.username}';
                this._updateCrumbs();
                new Future(() {
                    this.scope.broadcast('masonry.layout');
                });
            })
            .catchError((response) {
                this.error = response.data['message'];
            })
            .whenComplete(() {
                this.loading--;
                completer.complete();
            });

        return completer.future;
    }

    /// Request extraction of older posts for this profile.
    void fetchMorePosts(Event event, String data, function resetButton) {
        String pageUrl = '/api/profile/${this.id}/posts/fetch';
        this.api
            .get(pageUrl, needsAuth: true)
            .catchError((response) {
                this.error = response.data['message'];
            })
            .whenComplete(() {
                resetButton();
            });
    }

    /// Update breadcrumbs.
    void _updateCrumbs() {
        String username;

        if (this.username == null) {
            username = this.id;
        } else {
            username = this.username;
        }

        this.crumbs = [
            new Breadcrumb('QuickPin', '/'),
            new Breadcrumb('Profiles', '/profile'),
            new Breadcrumb(this.username, '/profile/${this.id}'),
            new Breadcrumb('Posts'),
        ];
    }
    
    /// Fetch worker jobs for profile.
    Future _fetchProfilePostsWorkers() {
        Completer completer = new Completer();

        this.api
            .get('/api/tasks/workers', needsAuth: true)
            .then((response) {
                this.workers = response.data['workers'];
                this.profilePostsWorkers = [];

                this._runningJobs = new Map<String, Map>();

                this.workers.forEach((worker) {
                    Map currentJob = worker['current_job'];

                    if (currentJob != null) {
                        if (currentJob['profile_id'] == int.parse(this.id)) {
                            if (currentJob['type'] == 'posts') {
                                this._runningJobs[currentJob['id']] = currentJob;
                                this.profilePostsWorkers.add(worker);

                            }
                        }
                    }
                });
            })
            .whenComplete(() {
                completer.complete();
            });

        return completer.future;
    }

    /// Fetch failed post task data for this profile.
    Future _fetchFailedProfilePostsTasks() {
        Completer completer = new Completer();
        List<Map> failed;

        this.api
            .get('/api/tasks/failed', needsAuth: true)
            .then((response) {
                failed = response.data['failed'];
                failed.forEach((failed_task) {
                    if (int.parse(this.id) == failed_task['profile_id']) {
                        if (failed_task['type'] == 'posts') {
                            this.failedTasks = true;
                        }
                    }
                });
                
            })
            .whenComplete(() {
                completer.complete();
            });

        return completer.future;
    }
}
