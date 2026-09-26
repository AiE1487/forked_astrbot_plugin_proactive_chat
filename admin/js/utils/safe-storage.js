/**
 * 文件职责：localStorage 安全封装。
 *
 * AstrBot 插件页面运行在沙箱 iframe（sandbox 无 allow-same-origin）中，
 * 此时访问 window.localStorage 会直接抛出 SecurityError；插件管理台因此
 * 统一通过本封装读写持久化键值：原生存储可用时直通，否则降级为内存 Map
 * （页面刷新后丢失，仅保证功能不中断）。
 */

(function () {
    'use strict';

    // 内存降级仓：原生 localStorage 不可用时的兜底存储。
    const memoryStore = new Map();
    // 探测结果缓存：null 表示原生存储不可用。
    let nativeStorage = null;

    // 通过一次写入/删除探测原生 localStorage 是否真正可用。
    try {
        const probeKey = '__proactive_storage_probe__';
        window.localStorage.setItem(probeKey, '1');
        window.localStorage.removeItem(probeKey);
        nativeStorage = window.localStorage;
    } catch (e) {
        nativeStorage = null;
    }

    const SafeStorage = {
        // 供启动诊断展示当前是否具备真实持久化能力。
        available: !!nativeStorage,

        getItem: function (key) {
            try {
                if (nativeStorage) return nativeStorage.getItem(key);
            } catch (e) {
                // 读取失败时按降级路径继续。
            }
            return memoryStore.has(key) ? memoryStore.get(key) : null;
        },

        setItem: function (key, value) {
            try {
                if (nativeStorage) {
                    nativeStorage.setItem(key, value);
                    return;
                }
            } catch (e) {
                // 写入失败（配额/隐私模式）时退回内存仓。
            }
            memoryStore.set(key, String(value));
        },

        removeItem: function (key) {
            try {
                if (nativeStorage) nativeStorage.removeItem(key);
            } catch (e) {
                // 删除失败时仍清理内存仓，保证语义一致。
            }
            memoryStore.delete(key);
        },
    };

    // 暴露为全局工具，供未经过 ESModule 打包的其余 JSX 文件直接调用。
    window.SafeStorage = SafeStorage;
})();
