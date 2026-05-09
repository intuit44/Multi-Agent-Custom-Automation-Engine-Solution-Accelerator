import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

// https://vitejs.dev/config/
export default defineConfig({
    plugins: [react()],

    // Define path aliases (similar to Create React App)
    resolve: {
        alias: {
            '@': resolve(__dirname, 'src'),
        },
    },



    // Server configuration
    server: {
        port: 3001,
        open: true,
        host: true,
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
                manualChunks: {
                    vendor: ['react', 'react-dom'],
                    fluentui: ['@fluentui/react-components', '@fluentui/react-icons'],
                    router: ['react-router-dom'],
                }
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
