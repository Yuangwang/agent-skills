---
name: firebase-app-hosting-basics
description: >-
  Deploys and manages full-stack web applications (Next.js, Angular) with Server-Side Rendering (SSR) using Firebase App Hosting. Use when deploying Next.js/Angular apps, configuring apphosting.yaml or firebase.json apphosting blocks, managing secrets, setting up GitHub CI/CD, or configuring Blaze billing requirements. Don't use for classic static web hosting, Auth, Firestore, Crashlytics, or Xcode.
metadata:
  category: Serverless
---

# App Hosting Basics

## Description

This skill enables the agent to deploy and manage modern, full-stack web
applications (Next.js, Angular, etc.) using Firebase App Hosting.

**Important**: In order to use App Hosting, your Firebase project must be on the
Blaze pricing plan. Direct the user to
https://console.firebase.google.com/project/_/overview?purchaseBillingPlan=metered
to upgrade their plan.

## Hosting vs App Hosting

**Choose Firebase Hosting if:**

- You are deploying a static site (HTML/CSS/JS).
- You are deploying a simple SPA (React, Vue, etc. without SSR).
- You want full control over the build and deploy process via CLI.

**Choose Firebase App Hosting if:**

- You are using a supported full-stack framework like Next.js or Angular.
- You need Server-Side Rendering (SSR) or ISR.
- You want an automated "git push to deploy" workflow with zero configuration.

## Deploying to App Hosting

### Deploy from Source

This is the recommended flow for most users and AI coding agents.

1. Configure `firebase.json` with an `apphosting` block:

   ```json
   {
     "apphosting": {
       "backendId": "my-app-id",
       "rootDir": "/",
       "ignore": [
         "node_modules",
         ".git",
         ".next",
         ".vercel",
         ".env*.local",
         "firebase-debug.log",
         "firebase-debug.*.log",
         "functions"
       ]
     }
   }
   ```

1. Create or edit `apphosting.yaml`- see
   [Configuration](references/configuration.md) for more information on how to
   do so (including migrating any required runtime environment variables or
   secrets from local `.env.local` / `.env` files, which are excluded from
   source uploads).

1. **Ensure the App Hosting backend exists before setting secrets or
   deploying:** In non-interactive/agent mode, `firebase deploy` cannot
   interactively prompt for a region to create a missing backend. First check
   `npx -y firebase-tools@latest apphosting:backends:list --project <project-id>`.
   If the backend does not exist yet, create it in a supported region (such as
   `us-central1` or `us-east4`; note that each region has a quota limit of 10
   backends per project):

   ```bash
   npx -y firebase-tools@latest apphosting:backends:create --backend my-app-id --primary-region us-central1 --root-dir / --project <project-id>
   ```

1. **Set and grant access to secrets (*after* the backend exists):** If the app
   needs safe access to sensitive keys in `apphosting.yaml`, set each secret
   non-interactively via stdin and explicitly grant backend access:

   ```bash
   printf "%s" "<secret-value>" | npx -y firebase-tools@latest apphosting:secrets:set <secret-name> --project <project-id> --data-file - --force
   npx -y firebase-tools@latest apphosting:secrets:grantaccess <secret-name> --backend my-app-id --project <project-id>
   ```

1. Run
   `npx -y firebase-tools@latest deploy --only apphosting --project <project-id>`
   (or `npx -y firebase-tools@latest deploy --project <project-id>`) when you
   are ready to deploy. Wait for `deploy` to complete—do **not** run
   `apphosting:rollouts:list` (it is an internal-only command not available in
   standard CLI installs).

### Automated deployment via GitHub (CI/CD)

Alternatively, set up a backend connected to a GitHub repository for automated
deployments "git push" deployments. This is only recommended for more advanced
users, and is not required to use App Hosting. See
[CLI Commands](references/cli_commands.md) for more information on how to set
this up using CLI commands.

## Emulation

See [Emulation](references/emulation.md) for more information on how to test
your app locally using the Firebase Local Emulator Suite.
