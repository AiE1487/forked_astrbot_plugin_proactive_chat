/**
 * 文件职责：轮询兜底版 WebSocket 替身（AstrBot 插件页面专用）。
 *
 * 插件 Pages 沙箱 iframe 中没有独立的 WebSocket 服务（v1.3.0 起移除），
 * 实时数据由 app.jsx 内置的 1 秒兜底轮询承担。本模块提供与 useWebSocket.js
 * 相同的全局 Hook 签名（订阅回调 + 清理函数），保证 app.jsx 无需感知环境。
 */

(function () {
    'use strict';

    function useWebSocket() {
        // 无实时通道可订阅；不做任何事，仅维持与原 Hook 相同的调用形态。
        React.useEffect(() => {}, []);
    }

    // 与 useWebSocket.js 保持一致的全局暴露方式。
    window.useWebSocket = useWebSocket;
})();
