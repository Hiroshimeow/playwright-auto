const path = require("path");
const root = __dirname;
module.exports = {
  apps: [
    {
      name: "playwright-display",
      cwd: root,
      script: path.join(root, "scripts/xvfb-start.sh"),
      interpreter: "none",
      autorestart: true,
      restart_delay: 2000,
    },
    {
      name: "playwright-selkies",
      cwd: root,
      script: path.join(root, "scripts/selkies-start.sh"),
      interpreter: "none",
      autorestart: true,
      restart_delay: 3000,
    },
    {
      name: "playwright-browser",
      cwd: root,
      script: path.join(root, "scripts/browser-gui.sh"),
      interpreter: "none",
      autorestart: true,
      restart_delay: 3000,
    },
  ],
};
