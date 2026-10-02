#!/bin/bash
#
# Railway Multi-Service Deployment Script
# 
# 此脚本帮助自动化部署服务（API、File Worker、MCP Server）到 Railway
#
# 使用方法:
#   1. 安装 Railway CLI: npm i -g @railway/cli
#   2. 登录: railway login
#   3. 运行此脚本: ./scripts/railway-deploy.sh
#
# 参考文档: https://docs.railway.com/guides/cli

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Railway Multi-Service Deployment${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

if ! command -v railway &> /dev/null; then
    echo -e "${RED}Error: Railway CLI 未安装${NC}"
    echo "请运行: npm i -g @railway/cli"
    exit 1
fi

if ! railway whoami &> /dev/null 2>&1; then
    echo -e "${YELLOW}请先登录 Railway...${NC}"
    railway login
fi

echo -e "${GREEN}✓ Railway CLI 已就绪${NC}"
echo ""

# ========== 步骤 1: 创建或关联项目 ==========
echo -e "${BLUE}步骤 1: 项目设置${NC}"
echo "----------------------------------------"
read -p "是否创建新项目? (y/n): " CREATE_NEW

if [[ "$CREATE_NEW" == "y" || "$CREATE_NEW" == "Y" ]]; then
    echo "创建新项目..."
    railway init
else
    echo "关联现有项目..."
    railway link
fi

echo -e "${GREEN}✓ 项目已关联${NC}"
echo ""

# ========== 步骤 2: 添加 Redis ==========
echo -e "${BLUE}步骤 2: 添加 Redis 数据库${NC}"
echo "----------------------------------------"
read -p "是否添加 Redis? (y/n): " ADD_REDIS

if [[ "$ADD_REDIS" == "y" || "$ADD_REDIS" == "Y" ]]; then
    echo "添加 Redis..."
    railway add
    echo -e "${GREEN}✓ Redis 已添加${NC}"
    echo ""
    echo -e "${YELLOW}重要: 请在 Railway Dashboard 中复制 Redis Internal URL${NC}"
    echo "格式: redis://default:xxx@redis.railway.internal:6379"
    echo ""
    read -p "按 Enter 继续..."
fi

echo ""

# ========== 步骤 3: 部署 API 服务 ==========
echo -e "${BLUE}步骤 3: 部署 API 服务${NC}"
echo "----------------------------------------"
echo "关联到 API Service..."
railway service

echo ""
echo -e "${YELLOW}请在 Railway Dashboard 中设置以下环境变量:${NC}"
cat << 'EOF'
SERVICE_ROLE=api

SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=your-key
S3_ENDPOINT_URL=https://...
S3_BUCKET_NAME=your-bucket
S3_ACCESS_KEY_ID=xxx
S3_SECRET_ACCESS_KEY=xxx
JWT_SECRET=your-secret
ETL_REDIS_URL=redis://...
DEBUG=False
EOF

echo ""
read -p "环境变量设置完成后，按 Enter 开始部署..."

echo "部署 API 服务..."
railway up

echo -e "${GREEN}✓ API 服务部署完成${NC}"
echo ""

# ========== 步骤 4: 创建 File Worker ==========
echo -e "${BLUE}步骤 4: 部署 File Worker${NC}"
echo "----------------------------------------"
echo -e "${YELLOW}请在 Railway Dashboard 中:${NC}"
echo "1. 点击 '+ New' → 'GitHub Repo' → 选择同一个仓库"
echo "2. Root Directory 设置为: backend"
echo "3. 服务重命名为: File Worker"
echo "4. 排空旧队列后设置环境变量: SERVICE_ROLE=upload_worker"
echo "5. 设置 OCR: OCR_PROVIDER=deepseek + DEEPINFRA_API_KEY"
echo "6. 复制 API Service 的其他环境变量"
echo ""
read -p "完成后按 Enter 继续..."

echo "关联到 File Worker..."
railway service

echo "部署 File Worker..."
railway up

echo -e "${GREEN}✓ File Worker 部署完成${NC}"
echo ""

# ========== 步骤 5 (可选): 创建 MCP Server ==========
echo -e "${BLUE}步骤 5 (可选): 部署 MCP Server${NC}"
echo "----------------------------------------"
read -p "是否部署 MCP Server? (y/n): " DEPLOY_MCP

if [[ "$DEPLOY_MCP" == "y" || "$DEPLOY_MCP" == "Y" ]]; then
    echo -e "${YELLOW}请在 Railway Dashboard 中:${NC}"
    echo "1. 点击 '+ New' → 'GitHub Repo' → 选择同一个仓库"
    echo "2. Root Directory 设置为: backend"
    echo "3. 服务重命名为: MCP Server"
    echo "4. 设置环境变量: SERVICE_ROLE=mcp_server"
    echo "5. 复制 API Service 的其他环境变量"
    echo ""
    read -p "完成后按 Enter 继续..."

    echo "关联到 MCP Server..."
    railway service

    echo "部署 MCP Server..."
    railway up

    echo -e "${GREEN}✓ MCP Server 部署完成${NC}"
fi

echo ""

# ========== 完成 ==========
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}  部署完成!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "请验证以下内容:"
echo "  1. API 健康检查: curl https://your-api.railway.app/health"
echo "  2. 查看 File Worker 日志: railway logs (关联到 File Worker)"
echo ""
echo "详细文档: backend/docs/RAILWAY_MULTI_SERVICE_DEPLOY.md"
