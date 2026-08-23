/**
 * VeroRun Visitor Profile Tracker - Lightweight frontend SDK
 * Tracks visitor behavior and sends events to the visitor_profile plugin.
 *
 * Usage:
 *   <script src="/admin/visitor_profile/static/js/tracker.js"></script>
 *   <script>
 *     VeroTracker.init({
 *       endpoint: '/admin/visitor_profile/ingest',
 *       autoTrack: true
 *     });
 *   </script>
 */
(function() {
  'use strict';

  var VeroTracker = {
    _config: {
      endpoint: '/admin/visitor_profile/ingest',
      autoTrack: true,
      batchSize: 10,
      flushInterval: 5000,  // ms
      trackClicks: true,
      trackForms: true,
      trackScroll: true,
      scrollThresholds: [25, 50, 75, 100],
      sessionIdleTimeout: 1800000  // 30 min
    },

    _state: {
      visitorId: null,
      sessionId: null,
      eventQueue: [],
      flushTimer: null,
      lastActivity: null,
      scrollMilestones: {},
      pageEnterTime: null,
      initialized: false
    },

    // ---------- Public API ----------

    init: function(options) {
      if (this._state.initialized) return;

      // Merge config
      if (options) {
        for (var k in options) {
          if (options.hasOwnProperty(k)) {
            this._config[k] = options[k];
          }
        }
      }

      // Generate or load visitor ID
      this._state.visitorId = this._getOrCreateVisitorId();
      this._state.sessionId = this._createSessionId();
      this._state.pageEnterTime = Date.now();

      // Auto-track if enabled
      if (this._config.autoTrack) {
        this._setupAutoTracking();
      }

      // Start flush timer
      this._startFlushTimer();

      // Track initial page view
      this.track('page_view', {
        page_url: window.location.href,
        page_title: document.title
      });

      // Flush on page unload
      var self = this;
      window.addEventListener('beforeunload', function() {
        self.flush(true);  // synchronous
      });

      this._state.initialized = true;
    },

    track: function(eventType, data) {
      var event = {
        event_type: eventType,
        page_url: window.location.href,
        page_title: document.title,
        event_data: data || {},
        session_id: this._state.sessionId,
        timestamp: new Date().toISOString()
      };

      this._state.eventQueue.push(event);
      this._state.lastActivity = Date.now();

      // Auto-flush if batch size reached
      if (this._state.eventQueue.length >= this._config.batchSize) {
        this.flush();
      }
    },

    flush: function(sync) {
      if (this._state.eventQueue.length === 0) return;

      var events = this._state.eventQueue.splice(0);
      var payload = JSON.stringify({
        visitor_id: this._state.visitorId,
        events: events
      });

      if (sync) {
        // Synchronous send (for beforeunload)
        var xhr = new XMLHttpRequest();
        xhr.open('POST', this._config.endpoint, false);
        xhr.setRequestHeader('Content-Type', 'application/json');
        try { xhr.send(payload); } catch(e) {}
      } else {
        // Async send with sendBeacon fallback
        if (navigator.sendBeacon) {
          var blob = new Blob([payload], {type: 'application/json'});
          navigator.sendBeacon(this._config.endpoint, blob);
        } else {
          var xhr = new XMLHttpRequest();
          xhr.open('POST', this._config.endpoint, true);
          xhr.setRequestHeader('Content-Type', 'application/json');
          xhr.send(payload);
        }
      }
    },

    identify: function(userId) {
      /**
       * Link visitor to a known user account.
       * Call this after user login/signup.
       */
      var payload = JSON.stringify({
        visitor_id: this._state.visitorId,
        user_id: userId,
        events: [{
          event_type: 'identify',
          event_data: { user_id: userId },
          session_id: this._state.sessionId,
          timestamp: new Date().toISOString()
        }]
      });

      if (navigator.sendBeacon) {
        var blob = new Blob([payload], {type: 'application/json'});
        navigator.sendBeacon(this._config.endpoint, blob);
      }
    },

    getVisitorId: function() {
      return this._state.visitorId;
    },

    // ---------- Private Methods ----------

    _getOrCreateVisitorId: function() {
      var key = '__vr_vid';
      var vid = localStorage.getItem(key);
      if (!vid) {
        vid = 'vr_' + this._uuid4();
        localStorage.setItem(key, vid);
      }
      return vid;
    },

    _createSessionId: function() {
      return 'sess_' + this._uuid4();
    },

    _uuid4: function() {
      return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(
        /[xy]/g, function(c) {
          var r = Math.random() * 16 | 0;
          return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    },

    _startFlushTimer: function() {
      var self = this;
      this._state.flushTimer = setInterval(function() {
        self.flush();
      }, this._config.flushInterval);
    },

    _setupAutoTracking: function() {
      var self = this;

      if (this._config.trackClicks) {
        document.addEventListener('click', function(e) {
          var el = e.target.closest('a, button, [data-track]');
          if (!el) return;

          var text = (el.textContent || '').trim().substring(0, 200);
          self.track('click', {
            element_id: el.id || null,
            element_text: text,
            element_tag: el.tagName.toLowerCase(),
            element_href: el.href || null
          });
        });
      }

      if (this._config.trackForms) {
        document.addEventListener('submit', function(e) {
          var form = e.target.closest('form');
          if (!form) return;

          // Collect non-sensitive field names
          var fields = [];
          var inputs = form.querySelectorAll('input, textarea, select');
          for (var i = 0; i < inputs.length; i++) {
            var inp = inputs[i];
            if (inp.name && inp.type !== 'password' && inp.type !== 'hidden') {
              fields.push(inp.name);
            }
          }

          self.track('form_submit', {
            form_id: form.id || null,
            form_action: form.action || null,
            field_names: fields
          });
        });
      }

      if (this._config.trackScroll) {
        var thresholds = this._config.scrollThresholds;
        window.addEventListener('scroll', self._throttle(function() {
          var scrollPercent = self._getScrollPercent();
          for (var i = 0; i < thresholds.length; i++) {
            var t = thresholds[i];
            if (scrollPercent >= t && !self._state.scrollMilestones[t]) {
              self._state.scrollMilestones[t] = true;
              self.track('scroll_depth', { depth_percent: t });
            }
          }
        }, 500));
      }
    },

    _getScrollPercent: function() {
      var h = document.documentElement;
      var b = document.body;
      var st = 'scrollTop', sh = 'scrollHeight';
      return Math.round(
        ((h[st] || b[st]) / ((h[sh] || b[sh]) - h.clientHeight)) * 100
      );
    },

    _throttle: function(fn, delay) {
      var last = 0;
      return function() {
        var now = Date.now();
        if (now - last >= delay) { last = now; fn(); }
      };
    }
  };

  // Expose to global scope
  window.VeroTracker = VeroTracker;
})();
