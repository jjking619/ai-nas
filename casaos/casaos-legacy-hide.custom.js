/* NAS_DEMO_LEGACY_HIDE_BEGIN */
(function () {
  var blacklist = {
    "openclaw": true,
    "knowledge_base": true,
    "media_downloader": true,
    "immich-server": true,
    "immich-machine-learning": true,
    "immich-postgres": true,
    "immich-redis": true
  };

  function shouldHideLegacyContainer(item) {
    if (!item || item.app_type !== "container") {
      return false;
    }
    var t = item.title || {};
    var name = String(t.en_us || t.en_US || "").trim().toLowerCase();
    return !!blacklist[name];
  }

  function filterAppGridPayload(payload) {
    if (!payload || !Array.isArray(payload.data)) {
      return payload;
    }
    payload.data = payload.data.filter(function (item) {
      return !shouldHideLegacyContainer(item);
    });
    return payload;
  }

  function isAppGridUrl(url) {
    return String(url || "").indexOf("/v2/app_management/web/appgrid") !== -1;
  }

  var rawFetch = window.fetch;
  if (typeof rawFetch === "function") {
    window.fetch = function (input, init) {
      return rawFetch(input, init).then(function (resp) {
        try {
          var url = typeof input === "string" ? input : (input && input.url) || "";
          if (!isAppGridUrl(url)) {
            return resp;
          }
          return resp.clone().json().then(function (obj) {
            var filtered = filterAppGridPayload(obj);
            return new Response(JSON.stringify(filtered), {
              status: resp.status,
              statusText: resp.statusText,
              headers: resp.headers
            });
          }).catch(function () {
            return resp;
          });
        } catch (_err) {
          return resp;
        }
      });
    };
  }

  var rawOpen = XMLHttpRequest.prototype.open;
  var rawSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this.__nasDemoUrl = url || "";
    return rawOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function () {
    var xhr = this;
    xhr.addEventListener("readystatechange", function () {
      try {
        if (xhr.readyState !== 4 || xhr.status !== 200 || !isAppGridUrl(xhr.__nasDemoUrl)) {
          return;
        }
        var raw = xhr.responseText;
        if (!raw) {
          return;
        }
        var parsed = JSON.parse(raw);
        var patched = JSON.stringify(filterAppGridPayload(parsed));
        Object.defineProperty(xhr, "responseText", {
          configurable: true,
          get: function () {
            return patched;
          }
        });
        Object.defineProperty(xhr, "response", {
          configurable: true,
          get: function () {
            return patched;
          }
        });
      } catch (_err) {
        return;
      }
    });
    return rawSend.apply(this, arguments);
  };
})();
/* NAS_DEMO_LEGACY_HIDE_END */
