<template>
  <div class="account-management">
    <div class="page-header">
      <h1>账号管理</h1>
    </div>
    
    <div class="account-tabs">
      <el-tabs v-model="activeTab" class="account-tabs-nav">
        <el-tab-pane label="全部" name="all">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="输入名称或账号搜索"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">添加账号</el-button>
                <!-- 定时续期开关：到点后由后端给下面这些账号逐个续一次会话，避免悄悄掉线。
                     组件自带状态（两页共享一份），设置页里也能开关同一件事。 -->
                <SessionRenewToggle />
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">刷新中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredAccounts.length > 0" class="account-list">
              <el-table :data="filteredAccounts" style="width: 100%">
                <el-table-column label="头像" width="80">
                  <template #default="scope">
                    <el-avatar :src="avatarSrc(scope.row)" :size="40" @error="() => onAvatarError(scope.row)" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="状态">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '验证中' ? 'is-loading' : ''" v-if="scope.row.status === '验证中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <!-- 异常账号的补救入口：不用先去点那个红色状态标签，这里直接重新授权一次。
                         只在异常时出现——正常账号没有重新扫码的必要，按钮常驻反而会让人误点，
                         而重新扫码会把当前登录态顶掉。 -->
                    <el-button
                      v-if="needsRelogin(scope.row.status)"
                      size="small"
                      type="warning"
                      :icon="Refresh"
                      @click="handleReLogin(scope.row)"
                    >
                      重新扫码
                    </el-button>
                    <!-- 「详情」：做进操作栏而不是新加表格列——粉丝/关注/获赞/作品数四个指标
                         塞进表格会让表宽到要横向滚动，而用户想看的是"这个账号最近怎么样"，
                         那需要一屏，不是一列。组件内部同时管着弹窗，所以这里只是一行。 -->
                    <AccountStatsButton :account="scope.row" />
                    <el-button size="small" @click="handleEdit(scope.row)">编辑</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下载Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上传Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="暂无账号数据" />
            </div>
          </div>
        </el-tab-pane>
        
        <el-tab-pane label="快手" name="kuaishou">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="输入名称或账号搜索"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">添加账号</el-button>
                <!-- 定时续期开关：到点后由后端给下面这些账号逐个续一次会话，避免悄悄掉线。
                     组件自带状态（两页共享一份），设置页里也能开关同一件事。 -->
                <SessionRenewToggle />
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">刷新中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredKuaishouAccounts.length > 0" class="account-list">
              <el-table :data="filteredKuaishouAccounts" style="width: 100%">
                <el-table-column label="头像" width="80">
                  <template #default="scope">
                    <el-avatar :src="avatarSrc(scope.row)" :size="40" @error="() => onAvatarError(scope.row)" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="状态">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '验证中' ? 'is-loading' : ''" v-if="scope.row.status === '验证中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <!-- 异常账号的补救入口：不用先去点那个红色状态标签，这里直接重新授权一次。
                         只在异常时出现——正常账号没有重新扫码的必要，按钮常驻反而会让人误点，
                         而重新扫码会把当前登录态顶掉。 -->
                    <el-button
                      v-if="needsRelogin(scope.row.status)"
                      size="small"
                      type="warning"
                      :icon="Refresh"
                      @click="handleReLogin(scope.row)"
                    >
                      重新扫码
                    </el-button>
                    <!-- 「详情」：做进操作栏而不是新加表格列——粉丝/关注/获赞/作品数四个指标
                         塞进表格会让表宽到要横向滚动，而用户想看的是"这个账号最近怎么样"，
                         那需要一屏，不是一列。组件内部同时管着弹窗，所以这里只是一行。 -->
                    <AccountStatsButton :account="scope.row" />
                    <el-button size="small" @click="handleEdit(scope.row)">编辑</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下载Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上传Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="暂无快手账号数据" />
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane label="拼多多视频" name="pdd">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="输入名称或账号搜索"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">添加账号</el-button>
                <!-- 定时续期开关：到点后由后端给下面这些账号逐个续一次会话，避免悄悄掉线。
                     组件自带状态（两页共享一份），设置页里也能开关同一件事。 -->
                <SessionRenewToggle />
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">刷新中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredPddAccounts.length > 0" class="account-list">
              <el-table :data="filteredPddAccounts" style="width: 100%">
                <el-table-column label="头像" width="80">
                  <template #default="scope">
                    <el-avatar :src="avatarSrc(scope.row)" :size="40" @error="() => onAvatarError(scope.row)" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="状态">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '验证中' ? 'is-loading' : ''" v-if="scope.row.status === '验证中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <!-- 异常账号的补救入口：不用先去点那个红色状态标签，这里直接重新授权一次。
                         只在异常时出现——正常账号没有重新扫码的必要，按钮常驻反而会让人误点，
                         而重新扫码会把当前登录态顶掉。 -->
                    <el-button
                      v-if="needsRelogin(scope.row.status)"
                      size="small"
                      type="warning"
                      :icon="Refresh"
                      @click="handleReLogin(scope.row)"
                    >
                      重新扫码
                    </el-button>
                    <!-- 「详情」：做进操作栏而不是新加表格列——粉丝/关注/获赞/作品数四个指标
                         塞进表格会让表宽到要横向滚动，而用户想看的是"这个账号最近怎么样"，
                         那需要一屏，不是一列。组件内部同时管着弹窗，所以这里只是一行。 -->
                    <AccountStatsButton :account="scope.row" />
                    <el-button size="small" @click="handleEdit(scope.row)">编辑</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下载Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上传Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="暂无拼多多视频账号数据" />
            </div>
          </div>
        </el-tab-pane>
        
        <el-tab-pane label="抖音" name="douyin">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="输入名称或账号搜索"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">添加账号</el-button>
                <!-- 定时续期开关：到点后由后端给下面这些账号逐个续一次会话，避免悄悄掉线。
                     组件自带状态（两页共享一份），设置页里也能开关同一件事。 -->
                <SessionRenewToggle />
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">刷新中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredDouyinAccounts.length > 0" class="account-list">
              <el-table :data="filteredDouyinAccounts" style="width: 100%">
                <el-table-column label="头像" width="80">
                  <template #default="scope">
                    <el-avatar :src="avatarSrc(scope.row)" :size="40" @error="() => onAvatarError(scope.row)" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="状态">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '验证中' ? 'is-loading' : ''" v-if="scope.row.status === '验证中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <!-- 异常账号的补救入口：不用先去点那个红色状态标签，这里直接重新授权一次。
                         只在异常时出现——正常账号没有重新扫码的必要，按钮常驻反而会让人误点，
                         而重新扫码会把当前登录态顶掉。 -->
                    <el-button
                      v-if="needsRelogin(scope.row.status)"
                      size="small"
                      type="warning"
                      :icon="Refresh"
                      @click="handleReLogin(scope.row)"
                    >
                      重新扫码
                    </el-button>
                    <!-- 「详情」：做进操作栏而不是新加表格列——粉丝/关注/获赞/作品数四个指标
                         塞进表格会让表宽到要横向滚动，而用户想看的是"这个账号最近怎么样"，
                         那需要一屏，不是一列。组件内部同时管着弹窗，所以这里只是一行。 -->
                    <AccountStatsButton :account="scope.row" />
                    <el-button size="small" @click="handleEdit(scope.row)">编辑</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下载Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上传Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="暂无抖音账号数据" />
            </div>
          </div>
        </el-tab-pane>
        
        <el-tab-pane label="视频号" name="channels">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="输入名称或账号搜索"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">添加账号</el-button>
                <!-- 定时续期开关：到点后由后端给下面这些账号逐个续一次会话，避免悄悄掉线。
                     组件自带状态（两页共享一份），设置页里也能开关同一件事。 -->
                <SessionRenewToggle />
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">刷新中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredChannelsAccounts.length > 0" class="account-list">
              <el-table :data="filteredChannelsAccounts" style="width: 100%">
                <el-table-column label="头像" width="80">
                  <template #default="scope">
                    <el-avatar :src="avatarSrc(scope.row)" :size="40" @error="() => onAvatarError(scope.row)" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="状态">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '验证中' ? 'is-loading' : ''" v-if="scope.row.status === '验证中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <!-- 异常账号的补救入口：不用先去点那个红色状态标签，这里直接重新授权一次。
                         只在异常时出现——正常账号没有重新扫码的必要，按钮常驻反而会让人误点，
                         而重新扫码会把当前登录态顶掉。 -->
                    <el-button
                      v-if="needsRelogin(scope.row.status)"
                      size="small"
                      type="warning"
                      :icon="Refresh"
                      @click="handleReLogin(scope.row)"
                    >
                      重新扫码
                    </el-button>
                    <!-- 「详情」：做进操作栏而不是新加表格列——粉丝/关注/获赞/作品数四个指标
                         塞进表格会让表宽到要横向滚动，而用户想看的是"这个账号最近怎么样"，
                         那需要一屏，不是一列。组件内部同时管着弹窗，所以这里只是一行。 -->
                    <AccountStatsButton :account="scope.row" />
                    <el-button size="small" @click="handleEdit(scope.row)">编辑</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下载Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上传Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="暂无视频号账号数据" />
            </div>
          </div>
        </el-tab-pane>
        
        <el-tab-pane label="小红书" name="xiaohongshu">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="输入名称或账号搜索"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">添加账号</el-button>
                <!-- 定时续期开关：到点后由后端给下面这些账号逐个续一次会话，避免悄悄掉线。
                     组件自带状态（两页共享一份），设置页里也能开关同一件事。 -->
                <SessionRenewToggle />
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">刷新中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredXiaohongshuAccounts.length > 0" class="account-list">
              <el-table :data="filteredXiaohongshuAccounts" style="width: 100%">
                <el-table-column label="头像" width="80">
                  <template #default="scope">
                    <el-avatar :src="avatarSrc(scope.row)" :size="40" @error="() => onAvatarError(scope.row)" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="状态">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '验证中' ? 'is-loading' : ''" v-if="scope.row.status === '验证中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <!-- 异常账号的补救入口：不用先去点那个红色状态标签，这里直接重新授权一次。
                         只在异常时出现——正常账号没有重新扫码的必要，按钮常驻反而会让人误点，
                         而重新扫码会把当前登录态顶掉。 -->
                    <el-button
                      v-if="needsRelogin(scope.row.status)"
                      size="small"
                      type="warning"
                      :icon="Refresh"
                      @click="handleReLogin(scope.row)"
                    >
                      重新扫码
                    </el-button>
                    <!-- 「详情」：做进操作栏而不是新加表格列——粉丝/关注/获赞/作品数四个指标
                         塞进表格会让表宽到要横向滚动，而用户想看的是"这个账号最近怎么样"，
                         那需要一屏，不是一列。组件内部同时管着弹窗，所以这里只是一行。 -->
                    <AccountStatsButton :account="scope.row" />
                    <el-button size="small" @click="handleEdit(scope.row)">编辑</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下载Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上传Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="暂无小红书账号数据" />
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
    
    <!-- 添加/编辑账号对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="reloginMode ? '重新扫码授权' : (dialogType === 'add' ? '添加账号' : '编辑账号')"
      width="500px"
      :close-on-click-modal="false"
      :close-on-press-escape="!sseConnecting"
      :show-close="!sseConnecting"
    >
      <el-form :model="accountForm" label-width="80px" :rules="rules" ref="accountFormRef">
        <el-form-item label="平台" prop="platform">
          <el-select 
            v-model="accountForm.platform" 
            placeholder="请选择平台" 
            style="width: 100%"
            :disabled="dialogType === 'edit' || sseConnecting"
          >
            <el-option label="快手" value="快手" />
            <el-option label="拼多多视频" value="拼多多视频" />
            <el-option label="抖音" value="抖音" />
            <el-option label="视频号" value="视频号" />
            <el-option label="小红书" value="小红书" />
          </el-select>
        </el-form-item>
        <!-- 拼多多视频的身份说明：它一套后台有三种身份，登录方式各不相同，而本工具只做达人。
             不写清楚的话，商家号主会拿商家账号去扫这个码，然后卡在"扫了没反应"。 -->
        <el-alert
          v-if="accountForm.platform === '拼多多视频'"
          type="info"
          :closable="false"
          show-icon
          class="platform-hint"
        >
          <template #title>
            拼多多视频走「达人」身份扫码（拼多多后台的商家 / 机构身份登录方式不同，本工具不支持）
          </template>
        </el-alert>
        <el-form-item label="名称" prop="name">
          <el-input 
            v-model="accountForm.name" 
            placeholder="请输入账号名称" 
            :disabled="sseConnecting"
          />
        </el-form-item>
        
        <!-- 二维码显示区域 -->
        <div v-if="sseConnecting" class="qrcode-container">
          <div v-if="qrCodeData && !loginStatus" class="qrcode-wrapper">
            <p class="qrcode-tip">请使用对应平台APP扫描二维码登录</p>
            <img :src="qrCodeData" alt="登录二维码" class="qrcode-image" />
          </div>
          <div v-else-if="!qrCodeData && !loginStatus" class="loading-wrapper">
            <el-icon class="is-loading"><Refresh /></el-icon>
            <span>请求中...</span>
          </div>
          <div v-else-if="loginStatus === '200'" class="success-wrapper">
            <el-icon><CircleCheckFilled /></el-icon>
            <span>添加成功</span>
          </div>
          <div v-else-if="loginStatus === '500'" class="error-wrapper">
            <el-icon><CircleCloseFilled /></el-icon>
            <span>添加失败，请稍后再试</span>
          </div>
        </div>

        <!-- 后端给的失败原因与下一步建议（含网页登录地址） -->
        <el-alert
          v-if="loginHint"
          class="login-hint"
          type="warning"
          :closable="false"
          show-icon
          title="登录没有完成"
          :description="loginHint"
        />
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button 
            type="primary" 
            @click="submitAccountForm" 
            :loading="sseConnecting" 
            :disabled="sseConnecting"
          >
            {{ sseConnecting ? '请求中' : '确认' }}
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { Refresh, CircleCheckFilled, CircleCloseFilled, Download, Upload, Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { accountApi } from '@/api/account'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { http } from '@/utils/request'
import { withToken } from '@/utils/session'
// 「定时续期」开关（放在「添加账号」旁边）。状态与「设置」页共享，
// 见 composables/useSessionRenew.js 里关于"为什么不做成 5 份各自的状态"的说明。
import SessionRenewToggle from '@/components/SessionRenewToggle.vue'
// 「详情」按钮 + 账号数据弹窗（粉丝 / 互动 / 作品）。抽成组件是因为本页五个 tab
// 各有一张自己的账号表，写五遍必然改一处漏四处。
import AccountStatsButton from '@/components/AccountStatsButton.vue'

// 获取账号状态管理
const accountStore = useAccountStore()
// 获取应用状态管理
const appStore = useAppStore()

// 当前激活的标签页
const activeTab = ref('all')

// 搜索关键词
const searchKeyword = ref('')

// 获取账号数据（快速，不验证）
const fetchAccountsQuick = async () => {
  try {
    const res = await accountApi.getAccounts()
    if (res.code === 200 && res.data) {
      // 照数据库里的状态显示。
      // 这里**不再**统一标成「验证中」：这一步只是读一次数据库（毫秒级），并没有在跑
      // 校验。标了的话进页面看到的就是"所有账号都在校验中"，而那个状态要等下面
      // verifyOneAccount 一条条请求回来才会消失——账号多、平台慢的时候能挂好几分钟，
      // 也就分不清"在查"还是"卡住了"。
      accountStore.setAccounts(res.data)
      resetAvatarCache()
    }
  } catch (error) {
    console.error('快速获取账号数据失败:', error)
  }
}

// 校验**一个**账号：一个账号一条请求，谁先回来谁先更新那一行。
//
// 这就是"各账号单独验证"：后端给每个账号开一个线程 + 一个浏览器（整机同时开几个由
// browser_lock 把关），账号之间互不影响。所以某个平台卡住（实测视频号那条要 29 秒）
// 不会把其它账号的结论一起拖住，界面上是一行一行变绿的，不是等最后一个。
//
// 返回 { name, status } 给调用方汇总用；status 取 ok / invalid / busy / error，
// 后两个是"这次没校验成"，和"确实失效"不是一回事。
const verifyOneAccount = async (account) => {
  let status = 'error'
  try {
    const res = await accountApi.getValidAccounts(account.id)
    const row = res?.data?.[0]
    status = res?.verify?.results?.[0]?.status || (res?.code === 200 ? 'ok' : 'error')
    if (res?.code === 200 && row) {
      // 回写后端刚给的状态（原始值，不是 '正常'/'异常'）：
      // 校验失败的账号后端已经写成 0；被占用 / 出错而**没校验成**的账号库里还是原值，
      // 界面就自然退回它原来的状态，不会永远停在「验证中」。
      accountStore.setAccountStatus(account.id, row[4])
      return { name: account.name, status }
    }
    accountStore.setAccountStatus(account.id, account.rawStatus)
  } catch (error) {
    console.error(`校验账号「${account.name}」失败:`, error)
    accountStore.setAccountStatus(account.id, account.rawStatus)
  }
  return { name: account.name, status }
}

// 校验并刷新**一个**账号（目前只有"上传 Cookie"用：换的只是它一个账号的登录态）。
//
// 为什么不用 `fetchAccounts()`：那是"把每个账号各开一次浏览器校验一遍"，为了一个账号
// 让其余账号陪着各转 20~30 秒毫无道理。
//
// **扫码登录成功不走这里**——那条路连一次校验都不做，见 refreshAccountsAfterLogin。
//
// 只碰这一行：其它行的状态一个字都不动（不会出现"全表一起转圈"）。
const refreshOneAccount = async (row) => {
  const rawStatus = row.status === '正常' ? 1 : 0
  resetAvatarCache() // 换了 cookie，头像可能也跟着换一张
  accountStore.setAccountStatus(row.id, -1) // -1 = 只把这一行标成「验证中」
  return verifyOneAccount({ id: row.id, name: row.name, rawStatus })
}

// 「账号数据」（含头像）是后端**后台线程**同步的（myUtils/login.py::_schedule_account_sync，
// 要开一次浏览器，十几秒到几十秒）。第一次读库时它多半还没写完，头像是 404 →
// 前端会把这个账号记进 avatarMissing（见 avatarSrc），于是先显示文字头像；
// 不再读一次的话，那个文字头像会一直挂到下一次刷新。
// 所以过一会儿再读一次库（只是读库，不开浏览器、不做校验），头像到位就自动换上。
const AVATAR_RECHECK_DELAY_MS = 20000

// 扫码成功后刷新列表：**只读库，不校验**。
//
// 登录流程本身已经带着这个账号真的进过后台了，再开一个浏览器去"确认它有效"
// 是把同一件事做第二遍（新账号 ~5 秒、视频号 ~30 秒）。而这一行该有的东西后端全写好了：
// 账号行和登录态由 `_record_account` 落库（新账号 `INSERT status=1`、重新扫码 `UPDATE status=1`），
// 账号数据由 `_schedule_account_sync` 同步——前端读库就是最新的。
const refreshAccountsAfterLogin = async () => {
  await fetchAccountsQuick()
  // 标记为已访问（和点刷新那条路保持一致）
  if (appStore.isFirstTimeAccountManagement) {
    appStore.setAccountManagementVisited()
  }
  setTimeout(fetchAccountsQuick, AVATAR_RECHECK_DELAY_MS)
}

// 刷新：把每个账号各校验一次。
//
// **只有用户点右上角那个刷新按钮才会走到这里**——进页面不校验（见 onMounted），
// 只动了一个账号的动作也不该走这里（见 refreshOneAccount）。
const fetchAccounts = async () => {
  if (appStore.isAccountRefreshing) return

  appStore.setAccountRefreshing(true)

  // 先记下每一行原来的状态：万一某条请求整个失败（后端没起来之类），要能退回它，
  // 不能把账号永远留在「验证中」——那个状态除了这一轮没有别人会去清。
  const snapshot = accountStore.accounts.map(account => ({
    id: account.id,
    name: account.name,
    rawStatus: account.status === '正常' ? 1 : 0
  }))
  accountStore.markAllVerifying()

  try {
    const results = await Promise.all(snapshot.map(account => verifyOneAccount(account)))
    // 刷新一次就给所有账号一次机会：刚同步到头像的账号不该因为之前那次 404 一直显示文字头像
    resetAvatarCache()

    const missed = results.filter(item => item.status === 'busy' || item.status === 'error')
    if (missed.length) {
      // 说清"没查成"而不是"失效了"：前者重试一次就好，后者得重新扫码，别混为一谈
      ElMessage.warning(`这些账号这次没能校验：${missed.map(item => item.name).join('、')}`)
    } else {
      ElMessage.success('账号数据获取成功')
    }
    // 标记为已访问
    if (appStore.isFirstTimeAccountManagement) {
      appStore.setAccountManagementVisited()
    }
  } finally {
    appStore.setAccountRefreshing(false)
  }
}

// 页面加载时获取账号数据
onMounted(() => {
  // 只读一次数据库，**不校验**。
  // 校验要给每个账号开一次浏览器（一个账号十几秒到几十秒，还得跟平台真实交互），
  // 进页面就跑等于每次打开这个页面都白等一轮。要查就点右上角那个刷新按钮。
  fetchAccountsQuick()
})

// 获取平台标签类型
const getPlatformTagType = (platform) => {
  const typeMap = {
    '快手': 'success',
    '抖音': 'danger',
    '视频号': 'warning',
    '小红书': 'info',
    // 拼多多用主色（primary 之外 Element Plus 没有第六种语义色，danger/success
    // 都被占了，'primary' 在这里是合法的 tag type，视觉上也不会和别的平台撞色）
    '拼多多视频': 'primary'
  }
  return typeMap[platform] || 'info'
}

// 账号是否需要重新授权。
// 只有「异常」这一档需要：它是 cookie 真失效了。「验证中」不算——它只是校验还没跑完，
// 这时候去重新扫码反而是多余的，还会把正在校验的会话顶掉。
const needsRelogin = (status) => status === '异常'

// 判断状态标签是否可点击：等价于"这个状态能触发重新登录"。
// 收成同一个判断，是为了别让「异常」这个字面量散落到模板各处——改状态名时漏改一处，
// 就会出现"标签点得动、按钮不出现"这类不一致。
const isStatusClickable = (status) => needsRelogin(status)

// 获取状态标签类型
const getStatusTagType = (status) => {
  if (status === '验证中') {
    return 'info'; // 验证中使用灰色
  } else if (status === '正常') {
    return 'success'; // 正常使用绿色
  } else {
    return 'danger'; // 无效使用红色
  }
}

// 处理状态点击事件
const handleStatusClick = (row) => {
  if (isStatusClickable(row.status)) {
    // 触发重新登录流程
    handleReLogin(row)
  }
}

// 过滤后的账号列表
const filteredAccounts = computed(() => {
  if (!searchKeyword.value) return accountStore.accounts
  return accountStore.accounts.filter(account =>
    account.name.includes(searchKeyword.value)
  )
})

// 按平台过滤的账号列表
const filteredKuaishouAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '快手')
})

const filteredDouyinAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '抖音')
})

const filteredChannelsAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '视频号')
})

const filteredXiaohongshuAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '小红书')
})

const filteredPddAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '拼多多视频')
})

// 搜索处理
const handleSearch = () => {
  // 搜索逻辑已通过计算属性实现
}

// 对话框相关
const dialogVisible = ref(false)
const dialogType = ref('add') // 'add' 或 'edit'
// 是否由「重新扫码」按钮发起。与 dialogType 正交：重新扫码走的仍是 edit 那套
// （平台/名称锁住不改），只是标题和文案要说"重新扫码"，别让用户以为自己在改资料。
const reloginMode = ref(false)
const accountFormRef = ref(null)

// 账号表单
const accountForm = reactive({
  id: null,
  name: '',
  platform: '',
  status: '正常'
})

// 表单验证规则
const rules = {
  platform: [{ required: true, message: '请选择平台', trigger: 'change' }],
  name: [{ required: true, message: '请输入账号名称', trigger: 'blur' }]
}

// SSE连接状态
const sseConnecting = ref(false)
const qrCodeData = ref('')
const loginHint = ref('')   // 后端推来的失败原因 + 下一步建议
const loginStatus = ref('')

// 添加账号
const handleAddAccount = () => {
  dialogType.value = 'add'
  reloginMode.value = false
  Object.assign(accountForm, {
    id: null,
    name: '',
    platform: '',
    status: '正常'
  })
  // 重置SSE状态
  sseConnecting.value = false
  qrCodeData.value = ''
  loginHint.value = ''
  loginStatus.value = ''
  dialogVisible.value = true
}

// 编辑账号
const handleEdit = (row) => {
  dialogType.value = 'edit'
  reloginMode.value = false
  Object.assign(accountForm, {
    id: row.id,
    name: row.name,
    platform: row.platform,
    status: row.status
  })
  dialogVisible.value = true
}

// 删除账号
const handleDelete = (row) => {
  ElMessageBox.confirm(
    `确定要删除账号 ${row.name} 吗？`,
    '警告',
    {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    }
  )
    .then(async () => {
      try {
        // 调用API删除账号
        const response = await accountApi.deleteAccount(row.id)

        if (response.code === 200) {
          // 从状态管理中删除账号
          accountStore.deleteAccount(row.id)
          ElMessage({
            type: 'success',
            message: '删除成功',
          })
        } else {
          ElMessage.error(response.msg || '删除失败')
        }
      } catch (error) {
        console.error('删除账号失败:', error)
        ElMessage.error('删除账号失败')
      }
    })
    .catch(() => {
      // 取消删除
    })
}

// 下载Cookie文件
const handleDownloadCookie = (row) => {
  // 从后端获取Cookie文件
  const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'
  const downloadUrl = `${baseUrl}/downloadCookie?filePath=${encodeURIComponent(row.filePath)}`

  // 创建一个隐藏的链接来触发下载
  const link = document.createElement('a')
  link.href = downloadUrl
  link.download = `${row.name}_cookie.json`
  link.target = '_blank'
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

// 上传Cookie文件
const handleUploadCookie = (row) => {
  // 创建一个隐藏的文件输入框
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.style.display = 'none'
  document.body.appendChild(input)

  input.onchange = async (event) => {
    const file = event.target.files[0]
    if (!file) return

    // 检查文件类型
    if (!file.name.endsWith('.json')) {
      ElMessage.error('请选择JSON格式的Cookie文件')
      document.body.removeChild(input)
      return
    }

    try {
      // 创建FormData对象
      const formData = new FormData()
      formData.append('file', file)
      formData.append('id', row.id)
      formData.append('platform', row.platform)

      // 使用统一的http封装发送上传请求
      const result = await http.upload('/uploadCookie', formData)

      ElMessage.success('Cookie文件上传成功')
      // 换的是**这一个**账号的登录态，就只校验它（别把所有账号都查一遍）
      refreshOneAccount(row)
    } catch (error) {
      ElMessage.error('Cookie文件上传失败')
    } finally {
      document.body.removeChild(input)
    }
  }

  input.click()
}

// 重新登录账号（异常账号的「重新扫码」按钮，以及点状态标签都走这里）
const handleReLogin = (row) => {
  // 设置表单信息：平台和名称锁住不改，只换登录态
  dialogType.value = 'edit'
  reloginMode.value = true
  Object.assign(accountForm, {
    id: row.id,
    name: row.name,
    platform: row.platform,
    status: row.status
  })

  // 重置SSE状态（loginHint 也要清：上次失败的原因不该留在这次的扫码框上面）
  sseConnecting.value = false
  qrCodeData.value = ''
  loginHint.value = ''
  loginStatus.value = ''

  // 显示对话框
  dialogVisible.value = true

  // 立即开始登录流程
  setTimeout(() => {
    connectSSE(row.platform, row.name)
  }, 300)
}

// 获取默认头像
const getDefaultAvatar = (name) => {
  // 兜底头像：用第三方文字头像服务按名字生成一张（本地没有同步到头像时用它）
  return `https://ui-avatars.com/api/?name=${encodeURIComponent(name)}&background=random`
}

// 同步过详情的账号，头像已经**下载到后端本地**了（avatars/<账号id>.<ext>），
// 这里优先读本地那张；没同步过 / 加载失败就回退成上面的文字头像。
//
// 为什么要走本地而不是直接用平台 CDN 地址：热链不一定放行、URL 可能带时效，
// 表现就是头像随机裂图（详见 myUtils/account_stats/avatar.py 的说明）。
const AVATAR_API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'
//: 本地头像缺失/加载失败的账号 id（失败过的就别再反复请求了）
const avatarMissing = reactive(new Set())

const avatarSrc = (row) => {
  if (!row || !row.id || avatarMissing.has(row.id)) {
    return getDefaultAvatar(row?.name || '')
  }
  // `<img>` 带不了请求头，登录态只能走查询串（和扫码登录那两个 SSE 一样）
  return withToken(`${AVATAR_API_BASE}/account-stats/avatar/${row.id}`)
}

const onAvatarError = (row) => {
  if (row && row.id) avatarMissing.add(row.id)
  return false // 阻止 el-avatar 自己的默认回退，交给 :src 重算
}

// 账号列表刷新后，重新给"之前失败过"的账号一次机会
// （同步完头像就该显示出来，不能因为一次 404 就永远显示文字头像）
const resetAvatarCache = () => avatarMissing.clear()

// SSE事件源对象
let eventSource = null

// 关闭SSE连接
const closeSSEConnection = () => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}

// 建立SSE连接
const connectSSE = (platform, name) => {
  // 关闭可能存在的连接
  closeSSEConnection()

  // 设置连接状态
  sseConnecting.value = true
  qrCodeData.value = ''
  loginStatus.value = ''

  // 获取平台类型编号
  const platformTypeMap = {
    '小红书': '1',
    '视频号': '2',
    '抖音': '3',
    '快手': '4',
    // 5 = 拼多多视频（多多视频）。这条 SSE 走的是「达人」扫码，见 myUtils/login.py
    '拼多多视频': '5'
  }

  const type = platformTypeMap[platform] || '1'

  // 创建SSE连接
  // EventSource 不能自定义请求头，登录态只能走查询串（见 utils/session.js 的 withToken）
  const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'
  const url = withToken(`${baseUrl}/login?type=${type}&id=${encodeURIComponent(name)}`)

  // 每次新连接都清一次，避免上一次的提示串到这一次
  qrCodeData.value = ''
  loginHint.value = ''
  loginStatus.value = ''

  eventSource = new EventSource(url)

  // 监听消息
  eventSource.onmessage = (event) => {
    const data = event.data

    // 顺序很重要：**先判状态码，再判二维码**。
    // 原来写成 if (还没有二维码) {...} else if (状态码) {...}，有两个问题：
    //   ① 状态码被挂在"还没有二维码"后面，二维码一到位就永远判不到登录结果；
    //   ② 抖音的二维码会失效刷新，新码推过来时因为"已经有码了"被丢掉，
    //      界面一直显示过期的那张，用户扫了没反应。
    // 后端推来的提示（失败原因 + 网页登录地址）。放在最前面判，
    // 因为它比二维码更紧急——用户照着它才能自己把登录走完。
    if (typeof data === 'string' && data.startsWith('msg:')) {
      loginHint.value = data.slice(4)
      return
    }
    if (data === '200' || data === '500') {
      loginStatus.value = data

      // 如果登录成功
      if (data === '200') {
        setTimeout(() => {
          // 关闭连接
          closeSSEConnection()

          // 1秒后关闭对话框并刷新列表
          setTimeout(() => {
            dialogVisible.value = false
            sseConnecting.value = false

            // 根据是否是重新登录显示不同提示
            ElMessage.success(dialogType.value === 'edit' ? '重新登录成功' : '账号添加成功')

            // 只读一次库把列表刷新出来。**不校验**：登录刚证过它有效，
            // 再开一次浏览器去确认是白等（新账号 ~5 秒、视频号 ~30 秒），
            // 而状态和账号数据后端都已经写好了（见 refreshAccountsAfterLogin）。
            refreshAccountsAfterLogin()
          }, 1000)
        }, 1000)
      } else {
        // 登录失败，关闭连接
        closeSSEConnection()

        // 2秒后重置状态，允许重试
        setTimeout(() => {
          sseConnecting.value = false
          qrCodeData.value = ''
          loginStatus.value = ''
        }, 2000)
      }
    }
    // 二维码。这里**不加 `!qrCodeData.value` 的条件**：抖音的二维码失效后会自动刷新，
    // 新码推过来必须覆盖旧的，否则界面一直显示过期那张，用户扫了没反应。
    else if (data && (data.startsWith('data:image') || data.startsWith('http') || data.length > 100)) {
      qrCodeData.value = data.startsWith('data:image') || data.startsWith('http')
        ? data
        : `data:image/png;base64,${data}`
    } else if (data) {
      console.warn('未知的 SSE 数据，已忽略:', String(data).slice(0, 60))
    }
  }

  // 监听错误
  eventSource.onerror = (error) => {
    console.error('SSE连接错误:', error)
    ElMessage.error('连接服务器失败，请稍后再试')
    closeSSEConnection()
    sseConnecting.value = false
  }
}

// 提交账号表单
const submitAccountForm = () => {
  accountFormRef.value.validate(async (valid) => {
    if (valid) {
      if (dialogType.value === 'add') {
        // 建立SSE连接
        connectSSE(accountForm.platform, accountForm.name)
      } else {
        // 编辑账号逻辑
        try {
          // 将平台名称转换为类型数字
          const platformTypeMap = {
            '小红书': 1,
            '视频号': 2,
            '抖音': 3,
            '快手': 4,
            '拼多多视频': 5
          };
          const type = platformTypeMap[accountForm.platform] || 1;

          const res = await accountApi.updateAccount({
            id: accountForm.id,
            type: type,
            userName: accountForm.name
          })
          if (res.code === 200) {
            // 更新状态管理中的账号
            const updatedAccount = {
              id: accountForm.id,
              name: accountForm.name,
              platform: accountForm.platform,
              status: accountForm.status // Keep the existing status
            };
            accountStore.updateAccount(accountForm.id, updatedAccount)
            ElMessage.success('更新成功')
            dialogVisible.value = false
            // 这里**不刷新、也不校验**：改名只动了 userName 这一列，登录态一点没变，
            // 上面那句 updateAccount 已经把界面改好了。原来这里调 fetchAccounts()，
            // 等于"改个名字 → 所有账号各开一次浏览器"，纯属白等。
          } else {
            ElMessage.error(res.msg || '更新账号失败')
          }
        } catch (error) {
          console.error('更新账号失败:', error)
          ElMessage.error('更新账号失败')
        }
      }
    } else {
      return false
    }
  })
}

// 组件卸载前关闭SSE连接
onBeforeUnmount(() => {
  closeSSEConnection()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

@keyframes rotate {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.account-management {
  .page-header {
    margin-bottom: 20px;
    
    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }
  }
  
  .account-tabs {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: $box-shadow-light;
    
    .account-tabs-nav {
      padding: 20px;
    }
  }
  
  .account-list-container {
    .account-search {
      display: flex;
      justify-content: space-between;
      margin-bottom: 20px;
      
      .el-input {
        width: 300px;
      }
      
      .action-buttons {
        display: flex;
        gap: 10px;
        
        .el-icon.is-loading {
          animation: rotate 1s linear infinite;
        }
      }
    }
    
    .account-list {
      margin-bottom: 20px;
    }
    
    .empty-data {
      padding: 40px 0;
    }
  }
  
  // 二维码容器样式
  .clickable-status {
    cursor: pointer;
    transition: all 0.3s;

    &:hover {
      transform: scale(1.05);
      box-shadow: 0 0 8px rgba(0, 0, 0, 0.15);
    }
  }

  .login-hint {
  margin-top: 12px;
  text-align: left;
}
.login-hint :deep(.el-alert__description) {
  word-break: break-all;
  line-height: 1.6;
}
/* 平台身份说明（目前只有拼多多视频用得上）：跟着平台下拉走，不要抢表单项的垂直间距 */
.platform-hint {
  margin: -8px 0 12px 80px;
  width: calc(100% - 80px);
}
.platform-hint :deep(.el-alert__title) {
  line-height: 1.5;
  font-size: 12px;
}
.qrcode-container {
    margin-top: 20px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 250px;
    
    .qrcode-wrapper {
      text-align: center;
      
      .qrcode-tip {
        margin-bottom: 15px;
        color: #606266;
      }
      
      .qrcode-image {
        max-width: 200px;
        max-height: 200px;
        border: 1px solid #ebeef5;
        background-color: black;
      }
    }
    
    .loading-wrapper, .success-wrapper, .error-wrapper {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 10px;
      
      .el-icon {
        font-size: 48px;
        
        &.is-loading {
          animation: rotate 1s linear infinite;
        }
      }
      
      span {
        font-size: 16px;
      }
    }
    
    .success-wrapper .el-icon {
      color: #67c23a;
    }
    
    .error-wrapper .el-icon {
      color: #f56c6c;
    }
  }
}
</style>