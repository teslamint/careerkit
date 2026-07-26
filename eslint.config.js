export default [
  {
    files: ["src/careerkit/jobs/console/static/**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        document: "readonly",
        fetch: "readonly",
        FormData: "readonly",
        URLSearchParams: "readonly"
      }
    },
    rules: {
      "no-unused-vars": "error"
    }
  }
];
