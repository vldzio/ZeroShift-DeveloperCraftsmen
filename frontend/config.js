// ZeroShift frontend configuration.
//
// Set API_URL to the base URL of your deployed API Gateway HTTP API. After
// `make deploy`, CloudFormation emits the endpoint in the stack outputs
// (look for the `ZeroShiftPart3` stack, `DenialAnalyzerApiEndpoint`).
//
// You can also override this at runtime by appending `?api=<url>` to the
// page URL, e.g. `index.html?api=https://abc123.execute-api.us-east-1.amazonaws.com`.
//
// Leave blank if you have not deployed yet — the page will prompt you to
// enter the URL in the header input field.

window.ZEROSHIFT_CONFIG = {
  API_URL: ""
};
