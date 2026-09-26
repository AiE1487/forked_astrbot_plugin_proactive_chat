/**
 * 文件职责：bridge 版 HTTP 工具模块（AstrBot 插件页面专用）。
 *
 * 管理台运行在 AstrBot 插件 Pages 的沙箱 iframe 中时，不能直接 fetch
 * 后端接口，必须通过 window.AstrBotPluginPage bridge 转发。本模块提供与
 * http.js 完全一致的 window.HttpUtil 接口（get/post/del），使上层业务
 * 代码无需感知运行环境差异：
 * - 前端统一以 /api/ 前缀书写路径，这里转换为插件内相对 endpoint；
 * - 后端固定返回 {"ok": true/false, ...}，ok=false 时抛出 Error(error)。
 */

(function () {
    'use strict';

    function getBridge() {
        const bridge = window.AstrBotPluginPage;
        if (!bridge || typeof bridge.apiGet !== 'function') {
            throw new Error('AstrBot 插件页 bridge 未就绪，请稍后重试或重新打开页面');
        }
        return bridge;
    }

    function toEndpoint(path) {
        // 现有前端统一以 /api/ 前缀书写路径，内嵌模式转换为插件内相对 endpoint。
        let p = String(path || '');
        if (p.indexOf('/api/') === 0) return p.slice(5);
        if (p.indexOf('api/') === 0) return p.slice(4);
        return p.replace(/^\/+/, '');
    }

    function unwrap(result) {
        // 后端约定：ok=false 表示业务失败，error 字段为可读文案。
        if (result && typeof result === 'object' && result.ok === false) {
            throw new Error(result.error || '请求失败');
        }
        return result;
    }

    window.HttpUtil = {
        get: function (url) {
            return Promise.resolve()
                .then(getBridge)
                .then((bridge) => bridge.apiGet(toEndpoint(url)))
                .then(unwrap);
        },
        post: function (url, body) {
            // bridge 层只支持 JSON body；空 body 统一发送空对象保持接口风格一致。
            return Promise.resolve()
                .then(getBridge)
                .then((bridge) => bridge.apiPost(toEndpoint(url), body || {}))
                .then(unwrap);
        },
        del: function (url) {
            // bridge 未提供 DELETE 语义；后端取消/清空类接口同时注册了 POST 方法。
            return Promise.resolve()
                .then(getBridge)
                .then((bridge) => bridge.apiPost(toEndpoint(url), {}))
                .then(unwrap);
        },
    };
})();
