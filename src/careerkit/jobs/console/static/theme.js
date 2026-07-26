const storedTheme = localStorage.getItem("jd-console-theme");
if (storedTheme === "dark" || storedTheme === "light") {
  document.documentElement.dataset.theme = storedTheme;
}
