"use strict";

try {
  const theme = localStorage.getItem("pta_theme");
  if (theme) {
    document.documentElement.setAttribute("data-theme", theme === "light" ? "light" : "dark");
  }
} catch (error) {
  // Theme persistence is optional when browser storage is unavailable.
}
