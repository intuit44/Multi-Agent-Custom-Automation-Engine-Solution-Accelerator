import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'
import { fileURLToPath } from 'url'

const srcPath = fileURLToPath(new URL('./src', import.meta.url))

const chunkGroups: Record<string, string[]> = {
    vendor: ['react', 'react-dom'],
    fluentui: ['@fluentui/react-components', '@fluentui/react-icons'],
    router: ['react-router-dom', 'react-router'],
}

// https://vitejs.dev/config/
export default defineConfig({
    plugins: [react()],

    // Define path aliases (similar to Create React App)
    resolve: {
        alias: {
            '@': resolve(srcPath),
        },
    },



    // Server configuration
    server: {
        port: 3001,
        open: true,
        host: true,
        // WSL2: el watcher nativo de chokidar no detecta cambios en /workspaces,
        // por eso HMR no tomaba los edits. Polling lo arregla (cuesta algo de CPU
        // en idle, pero elimina el ritual de reiniciar vite tras cada cambio).
        watch: { usePolling: true, interval: 700 },
        proxy: {
            '/api': {
                target: 'http://localhost:8000',
                changeOrigin: true,
                secure: false,
                ws: true,
            },
            '/config': {
                target: 'http://localhost:8000',
                changeOrigin: true,
                secure: false,
            },
            '/inspector': {
                target: 'http://localhost:6274',
                changeOrigin: true,
                secure: false,
                rewrite: (path: string) => path.replace(/^\/inspector/, ''),
            },
        },
    },

    // Build configuration
    build: {
        outDir: 'build',
        sourcemap: true,
        // Optimize dependencies
        rollupOptions: {
            output: {
                manualChunks(id) {
                    if (!id.includes('node_modules')) {
                        return undefined
                    }

                    for (const [chunkName, packages] of Object.entries(chunkGroups)) {
                        if (packages.some((packageName) => id.includes(`/node_modules/${packageName}/`))) {
                            return chunkName
                        }
                    }

                    return undefined
                },
            }
        }
    },

    // Handle CSS and static assets
    css: {
        modules: {
            localsConvention: 'camelCase'
        }
    },

    // Environment variables configuration
    envPrefix: 'REACT_APP_',

    // Optimization
    optimizeDeps: {
        include: [
            'react',
            'react-dom',
            '@fluentui/react-components',
            '@fluentui/react-icons',
            'react-router-dom',
            'axios'
        ]
    }
})
