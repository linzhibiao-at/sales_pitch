<template>
  <div class="layout">
    <!-- 左侧导航 -->
    <nav class="sidebar">
      <div class="logo">
        <span class="logo-icon">💬</span>
        <span class="logo-text">FILA 话术助手</span>
      </div>
      <ul class="nav-list">
        <li>
          <RouterLink to="/generate" class="nav-item" active-class="active">
            <span class="nav-icon">✍️</span>
            <span>话术生成</span>
          </RouterLink>
        </li>
        <li>
          <RouterLink to="/audit" class="nav-item" active-class="active">
            <span class="nav-icon">📋</span>
            <span>审计记录</span>
          </RouterLink>
        </li>
        <li>
          <RouterLink to="/settings" class="nav-item" active-class="active">
            <span class="nav-icon">⚙️</span>
            <span>设置</span>
          </RouterLink>
        </li>
      </ul>
    </nav>
    <!-- 主内容区 -->
    <main class="main-content">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  min-height: 100vh;
}

/* 侧边栏 */
.sidebar {
  width: 220px;
  min-height: 100vh;
  background: #fff;
  box-shadow: 1px 0 0 #e8eaf0;
  display: flex;
  flex-direction: column;
  padding: 0 0 20px;
  position: fixed;
  top: 0;
  left: 0;
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 22px 20px 18px;
  border-bottom: 1px solid #f0f0f5;
}
.logo-icon { font-size: 22px; }
.logo-text { font-size: 15px; font-weight: 700; color: #1a1a2e; letter-spacing: 0.3px; }

.nav-list { list-style: none; padding: 12px 10px; display: flex; flex-direction: column; gap: 4px; }

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 14px;
  color: #555;
  transition: all 0.15s;
}
.nav-item:hover { background: #f5f6fa; color: #1a73e8; }
.nav-item.active { background: #e8f0fe; color: #1a73e8; font-weight: 600; }
.nav-icon { font-size: 16px; }

/* 主内容 */
.main-content {
  flex: 1;
  margin-left: 220px;
  padding: 28px 32px;
  max-width: 1200px;
}

@media (max-width: 768px) {
  .sidebar { width: 60px; }
  .logo-text, .nav-item span:last-child { display: none; }
  .main-content { margin-left: 60px; padding: 16px; }
}
</style>
