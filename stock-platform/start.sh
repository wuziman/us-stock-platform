#!/bin/bash

echo "================================"
echo "  量化交易平台启动脚本"
echo "  (首次运行请先执行 ./install.sh)"
echo "================================"

# 启动后端
echo ""
echo "[1/2] 启动后端服务..."
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

# 等待后端启动
echo "等待后端启动..."
sleep 3

# 启动前端
echo ""
echo "[2/2] 启动前端服务..."
cd ../frontend
npm run dev &
FRONTEND_PID=$!

echo ""
echo "================================"
echo "  服务启动完成！"
echo "================================"
echo ""
echo "后端API文档: http://localhost:8000/docs"
echo "前端界面:    http://localhost:3000"
echo ""
echo "按 Ctrl+C 停止所有服务"

# 捕获退出信号
trap "kill $BACKEND_PID $FRONTEND_PID; exit" SIGINT SIGTERM

# 等待
wait
