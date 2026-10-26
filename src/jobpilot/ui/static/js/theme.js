(function () {
  try {
    var stored = localStorage.getItem("jobpilot-theme");
    if (stored === "light" || stored === "dark") {
      document.documentElement.setAttribute("data-theme", stored);
    }
  } catch (error) {
    /* localStorage can be unavailable in hardened browsers. */
  }
})();
