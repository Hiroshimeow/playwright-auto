const path = require("path");
const root = __dirname;
module.exports = {
  apps: [
    {
      name: "playwright-kasmvnc",
      cwd: root,
      script: path.join(root, "scripts/kasmvnc-start.sh"),
      interpreter: "none",
      autorestart: true,
      restart_delay: 2000,
    },
    {
      name: "playwright-browser",
      cwd: root,
      script: path.join(root, "scripts/browser-gui.sh"),
      interpreter: "none",
      autorestart: true,
      restart_delay: 2000,
      wait_ready: false,
    },
  ],
};
