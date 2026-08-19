// Self-hosted WebMKS viewer.
//
// Only runs in `webmks` mode, and only when the operator has supplied VMware's
// wmks.js — that library is not redistributable, so it cannot be bundled and
// this file must not assume it exists.
(function () {
  "use strict";

  var mount = document.getElementById("console-canvas");
  if (!mount) return;

  if (typeof WMKS === "undefined") {
    mount.textContent =
      "The console viewer library did not load. Check HOSTPLANE_CONSOLE_WMKS_PATH.";
    mount.classList.add("alert", "alert-bad");
    return;
  }

  var url = mount.dataset.url;
  if (!url) {
    mount.textContent = "This console session has no connection URL.";
    mount.classList.add("alert", "alert-bad");
    return;
  }

  try {
    var wmks = WMKS.createWMKS("console-canvas", {
      rescale: true,
      changeResolution: false,
      position: WMKS.CONST.Position.CENTER,
    });
    wmks.register(WMKS.CONST.Events.CONNECTION_STATE_CHANGE, function (_event, data) {
      if (data.state === WMKS.CONST.ConnectionState.DISCONNECTED) {
        mount.classList.add("console-disconnected");
      }
    });
    wmks.connect(url);
  } catch (err) {
    mount.textContent = "Could not start the console session: " + err.message;
    mount.classList.add("alert", "alert-bad");
  }
})();
