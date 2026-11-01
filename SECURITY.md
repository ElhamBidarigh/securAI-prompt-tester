# Security Policy

## Supported versions

Only the latest release receives fixes. This is a small, solo-maintained open source project, so there is no formal response time, but reports are read and answered as soon as possible.

## Reporting a vulnerability in this tool

Please do not open a public issue with exploit details.

1. Use GitHub's private reporting: open the **Security** tab of this repository and choose **Report a vulnerability**.
2. If that option is not available, open a public issue titled "Security contact request" that contains no technical details, and a private channel will be arranged.

Useful reports include the version (`prompt-tester --version`), the command you ran, what you expected, and what happened.

## Responsible use

- This tool sends requests to the URL or model API you give it. Test only apps and API accounts you own, or that you have written permission to test. The `--authorized` flag exists to make that decision explicit.
- All built-in payloads are non-destructive. They try to make an app print a random harmless token or repeat a phrase from its own system prompt, and the data-leak test points at the reserved `.invalid` domain.
- API keys are read from environment variables and sent only in the request header. They are never written to reports, SARIF files or the console. Do not paste keys into issues or pull requests.
- A high score is not a certification. It covers a fixed set of basic attacks.
