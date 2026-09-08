/* 背景图样式 */

/* 默认：无背景 */
body {
  background-image: var(--bg-img, none);
  background-size: cover;
  background-position: center;
  background-attachment: fixed;
  background-repeat: no-repeat;
}

/* 遮罩层，让文字在背景上可读 */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background: var(--bg-overlay, transparent);
  pointer-events: none;
  z-index: -1;
}

/* 根据主题和选择显示不同背景 */
html[data-theme='light'] body,
html:not([data-theme]) body {
  --bg-overlay: rgba(250, 249, 245, 0.85);
}

html[data-theme='dark'] body {
  --bg-overlay: rgba(38, 38, 36, 0.88);
}

/* 背景选择 */
.bg-none { --bg-img: none; }
.bg-light { --bg-img: url('bg/light-bg.png'); }
.bg-dark { --bg-img: url('bg/dark-bg.png'); }
