/**
 * 阿里巴巴API管理控制台 - 前端交互
 * Admin dark theme version (no Bootstrap dependency)
 */
let currentPage = 'dashboard';
let currentProductPage = 1;
let currentLogPage = 1;
let currentAiItemId = null;

// ===== auth =====
const _pageToken = new URLSearchParams(location.search).get('token') || '';
axios.interceptors.request.use(function(config) {
    const csrfToken = getCookie('csrf_token');
    if (csrfToken) config.headers['X-CSRF-Token'] = csrfToken;
    if (_pageToken) config.headers['Authorization'] = 'Bearer ' + _pageToken;
    return config;
}, function(error) { return Promise.reject(error); });

function getCookie(name) {
    const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return match ? decodeURIComponent(match[2]) : null;
}

// ===== modal helpers =====
function showModal(id) {
    var el = document.getElementById(id);
    if (el) el.className = 'mo show';
}
function closeModal(id) {
    var el = document.getElementById(id);
    if (el) el.className = 'mo';
}

// ===== utils =====
function showLoading() {
    var el = document.getElementById('lo');
    if (el) el.className = 'lo show';
}
function hideLoading() {
    var el = document.getElementById('lo');
    if (el) el.className = 'lo';
}
function showMessage(elementId, message, type) {
    var el = document.getElementById(elementId);
    if (!el) return;
    var cls = type === 'error' ? 'msg err' : type === 'success' ? 'msg ok' : 'msg info';
    el.innerHTML = '<div class="'+cls+'">'+message+'</div>';
}
function formatPrice(p) {
    if (p===null||p===undefined) return __('Unpriced');
    return '¥'+parseFloat(p).toFixed(2);
}
function formatDate(d) {
    if (!d) return '-';
    try { return new Date(d).toLocaleString(); } catch(e) { return d; }
}
function getStatusBadge(status) {
    var m = {
        'active': '<span class="stbd on">' + __('Active') + '</span>',
        'inactive': '<span class="stbd off">' + __('Inactive') + '</span>',
        'draft': '<span class="bdg gy">' + __('Draft') + '</span>',
        'published': '<span class="bdg g">' + __('Published') + '</span>',
        'unpublished': '<span class="bdg y">' + __('Unpublished') + '</span>',
        'failed': '<span class="bdg r">' + __('Failed') + '</span>',
    };
    return m[status] || '<span class="bdg gy">'+status+'</span>';
}

// ===== navigation =====
document.addEventListener('DOMContentLoaded', function() {
    initNavigation();
    loadDashboard();

    document.getElementById('collect-single-btn')?.addEventListener('click', collectSingleProduct);
    document.getElementById('search-collect-btn')?.addEventListener('click', searchCollect);
    document.getElementById('ai-optimize-btn')?.addEventListener('click', async function() {
        var id = document.getElementById('ai-product-id')?.value?.trim();
        if (!id) { showMessage('ai-result', __('Please enter the product ID'), 'error'); return; }
        var n = parseInt(id);
        if (isNaN(n)) { showMessage('ai-result', __('Please enter a numeric ID'), 'error'); return; }
        await generateAiTitles(n);
    });
    document.getElementById('product-search')?.addEventListener('keypress', function(e) { if (e.key==='Enter') loadProducts(1); });
    document.getElementById('status-filter')?.addEventListener('change', function() { loadProducts(1); });
    document.getElementById('filter-logs-btn')?.addEventListener('click', function() { loadLogs(1); });
    document.getElementById('log-endpoint')?.addEventListener('keypress', function(e) { if (e.key==='Enter') loadLogs(1); });
    document.getElementById('refresh-cache-stats')?.addEventListener('click', loadCacheStats);
    document.getElementById('cache-type')?.addEventListener('change', function() {
        var c = document.getElementById('product-id-container');
        if (c) c.style.display = this.value === 'product' ? 'block' : 'none';
    });
    document.getElementById('clear-cache-btn')?.addEventListener('click', async function() {
        if (!confirm(__('Confirm cache clear?'))) return;
        var type = document.getElementById('cache-type')?.value || 'all';
        var productId = document.getElementById('cache-product-id')?.value?.trim() || '';
        var data = { type: type };
        if (type==='product' && productId) data.product_id = productId;
        try {
            showLoading();
            var res = await axios.post('/admin/ali-api/cache/clear', data);
            if (res.data.success) { showMessage('clear-result', res.data.message, 'success'); loadCacheStats(); if (document.getElementById('cache-product-id')) document.getElementById('cache-product-id').value=''; }
            else { showMessage('clear-result', __('Cleanup failed') + ': ' + res.data.error, 'error'); }
        } catch(e) { showMessage('clear-result', __('Cleanup failed'), 'error'); }
        finally { hideLoading(); }
    });
    // upload zone
    var uz = document.getElementById('upload-zone');
    var ui = document.getElementById('image-upload-input');
    if (uz && ui) {
        uz.addEventListener('click', function() { ui.click(); });
        ui.addEventListener('change', function() { if (this.files&&this.files.length>0&&_galleryItemId) { uploadGalleryImages(_galleryItemId, this.files); this.value=''; } });
        uz.addEventListener('dragover', function(e) { e.preventDefault(); this.style.borderColor='var(--accent)'; this.style.background='rgba(0,245,255,0.05)'; });
        uz.addEventListener('dragleave', function(e) { e.preventDefault(); this.style.borderColor=''; this.style.background='var(--bg)'; });
        uz.addEventListener('drop', function(e) { e.preventDefault(); this.style.borderColor=''; this.style.background='var(--bg)'; if (e.dataTransfer.files&&e.dataTransfer.files.length>0&&_galleryItemId) uploadGalleryImages(_galleryItemId, e.dataTransfer.files); });
    }
});

function initNavigation() {
    document.querySelectorAll('.tb-i').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var page = this.getAttribute('data-page');
            document.querySelectorAll('.tb-i').forEach(function(b) { b.classList.remove('act'); });
            this.classList.add('act');
            document.querySelectorAll('.page').forEach(function(el) { el.style.display='none'; });
            var p = document.getElementById(page+'-page');
            if (p) p.style.display='block';
            currentPage = page;
            loadPageData(page);
        });
    });
}
function loadPageData(page) {
    switch(page) {
        case 'dashboard': loadDashboard(); break;
        case 'products': loadProducts(); break;
        case 'logs': loadLogs(); break;
        case 'cache': loadCacheStats(); break;
        case 'config': loadConfig(); break;
        case 'supplier': loadPurchaseOrders(); break;
    }
}

// ===== dashboard =====
async function loadDashboard() {
    try {
        showLoading();
        var res = await axios.get('/admin/ali-api/dashboard');
        if (!res.data.success) return;
        var d = res.data.data;
        document.getElementById('total-items').textContent = d.items.total;
        document.getElementById('active-items').textContent = d.items.active;
        document.getElementById('total-calls').textContent = d.api_calls.total;
        document.getElementById('today-calls').textContent = d.api_calls.today;
        document.getElementById('total-users').textContent = d.users.total;
        updateRateLimitStats(d.rate_limit);
        updateCacheStats(d.cache);
    } catch(e) { console.error('dashboard load failed', e); }
    finally { hideLoading(); }
}
function updateRateLimitStats(s) {
    var el = document.getElementById('rate-limit-stats');
    if (!el || !s) return;
    el.innerHTML = '<div><div style="font-size:11px;color:var(--dim)">' + __('User rate limiting') + '</div><div style="font-size:12px;color:var(--muted);margin-top:4px">' + __('Daily remaining') + ': ' + (s.user_limits?.daily_remaining??'-') + '/' + (s.user_limits?.daily_limit??'-') + '<br>' + __('Hourly remaining') + ': ' + (s.user_limits?.hourly_remaining??'-') + '/' + (s.user_limits?.hourly_limit??'-') + '</div></div><div><div style="font-size:11px;color:var(--dim)">' + __('Concurrency control') + '</div><div style="font-size:12px;color:var(--muted);margin-top:4px">' + __('Active requests') + ': ' + (s.concurrent?.active_requests??0) + '/' + (s.concurrent?.max_concurrent??'-') + '<br>' + __('Current QPS') + ': ' + (s.concurrent?.current_qps??0) + '/' + (s.concurrent?.qps_limit??'-') + '</div></div>';
}
function updateCacheStats(s) {
    var el = document.getElementById('cache-stats');
    if (!el || !s) return;
    var rc = s.redis?.connected;
    el.innerHTML = '<div><div style="font-size:11px;color:var(--dim)">Redis</div><div style="font-size:12px;color:var(--muted);margin-top:4px">' + __('Status') + ': ' + (rc?'<span class="stbd on">' + __('Connected') + '</span>':'<span class="stbd off">' + __('Not connected') + '</span>') + (rc?'<br>' + __('Memory') + ': ' + (s.redis.used_memory||'N/A'):'<br>' + __('Using memory cache')) + '</div></div><div><div style="font-size:11px;color:var(--dim)">' + __('Memory cache') + '</div><div style="font-size:12px;color:var(--muted);margin-top:4px">' + __('Entries') + ': ' + (s.memory?.size??0) + '/' + (s.memory?.maxsize??'-') + '<br>TTL: ' + (s.memory?.ttl??'-') + __('s') + '</div></div>';
}

// ===== products =====
async function loadProducts(page) {
    if (page===undefined) page=currentProductPage;
    try {
        showLoading();
        var status = document.getElementById('status-filter')?.value||'active';
        var kw = document.getElementById('product-search')?.value||'';
        var url = '/admin/ali-api/items?page='+page+'&per_page=20&status='+status;
        if (kw) url+='&keyword='+encodeURIComponent(kw);
        var res = await axios.get(url);
        if (res.data.success) { updateProductsTable(res.data.data.items); updateProductsPagination(res.data.data.pagination); currentProductPage=page; }
    } catch(e) { console.error('load products failed', e); showMessage('products-table', __('Failed to load products'), 'error'); }
    finally { hideLoading(); }
}
function updateProductsTable(items) {
    var tbody = document.getElementById('products-table');
    if (!tbody) return;
    if (!items||items.length===0) { tbody.innerHTML='<tr><td colspan="8" class="tc dim" style="padding:20px">' + __('No products') + '</td></tr>'; return; }
    tbody.innerHTML = items.map(function(item) {
        var imgs = parseJsonField(item.images, []);
        var imgH = imgs.length>0 ? '<img src="'+escHtml(imgs[0])+'" class="pr-img" alt="' + __('Image') + '" onerror="safeImgOnError.call(this)">' : '<div class="pr-img no">' + __('No image') + '</div>';
        return '<tr><td>'+item.id+'</td><td>'+imgH+'</td><td><strong>'+escHtml(item.title||item.original_title||__('No title'))+'</strong><br><span class="dim" style="font-size:10px">ID: '+escHtml(item.product_id)+'</span></td><td>'+formatPrice(item.price)+'</td><td>'+escHtml(item.category||__('Uncategorized'))+'</td><td>'+getStatusBadge(item.status)+'<br>'+getStatusBadge(item.publish_status)+'</td><td>'+formatDate(item.updated_at)+'</td><td><div style="display:flex;gap:3px">'+
            '<button class="btn bs" title="' + __('View') + '" onclick="viewProduct('+item.id+')">&#x1F441;</button>'+
            '<button class="btn bs" title="' + __('Image') + '" onclick="openImageGallery('+item.id+')">&#x1F5BC;</button>'+
            '<button class="btn bs" title="' + __('AI Title') + '" onclick="generateAiTitles('+item.id+')">&#x2728;</button>'+
            '<button class="btn bs" title="' + __('Publish') + '" onclick="publishProduct('+item.id+')"'+(item.publish_status==='published'?' disabled':'')+'>&#x2B06;</button>'+
            '</div></td></tr>';
    }).join('');
}
function updateProductsPagination(p) {
    var el = document.getElementById('products-pagination');
    if (!el) return;
    var page=p.page, total=p.total_pages;
    var h='';
    h+=page>1?'<a onclick="loadProducts('+(page-1)+');return false;">' + __('Previous') + '</a>':'<a class="dis">' + __('Previous') + '</a>';
    var start=Math.max(1,page-2), end=Math.min(total,page+2);
    for(var i=start;i<=end;i++) h+=i===page?'<a class="act">'+i+'</a>':'<a onclick="loadProducts('+i+');return false;">'+i+'</a>';
    h+=page<total?'<a onclick="loadProducts('+(page+1)+');return false;">' + __('Next') + '</a>':'<a class="dis">' + __('Next') + '</a>';
    el.innerHTML=h;
}
async function viewProduct(itemId) {
    try {
        var res = await axios.get('/admin/ali-api/items/'+itemId);
        if (!res.data.success) { alert(__('Failed to fetch') + ': ' + res.data.error); return; }
        var p = res.data.data;
        var specs = parseJsonField(p.specs, {});
        var sku = parseJsonField(p.product_sku, []);
        var images = parseJsonField(p.images, []);
        var titleOpts = parseJsonField(p.ai_title_options, []);
        var detail = 'ID: ' + p.id + '\n' + __('Product ID') + ': ' + p.product_id + '\n' + __('Title') + ': ' + (p.title||p.original_title||'-') + '\n';
        if (p.ai_title) detail += __('AI Title') + ': ' + p.ai_title + '\n';
        if (p.selected_title) detail += __('Selected title') + ': ' + p.selected_title + '\n';
        detail += __('Price') + ': ' + formatPrice(p.price) + '\n' + __('Original price') + ': ' + formatPrice(p.original_price) + '\n' + __('Category') + ': ' + (p.category||'-') + '\n' + __('Status') + ': ' + p.status + ' | ' + __('Publish') + ': ' + p.publish_status + '\n' + __('Image') + ': ' + images.length + ' ' + __('pics');
        if (p.target_product_id) detail += '\n' + __('Local product ID') + ': ' + p.target_product_id;
        if (Object.keys(specs).length>0) detail += '\n' + __('Specifications') + ': ' + JSON.stringify(specs,null,2);
        if (sku.length>0) detail += '\n' + __('SKU') + ': ' + sku.length + ' ' + __('items');
        if (images.length>0) { detail += '\n\n' + __('Image list') + ':'; images.forEach(function(img,i){ var u=typeof img==='string'?img:(img.url||''); detail+='\n  '+(i+1)+'. '+u.substring(0,60)+(u.length>60?'...':''); }); }
        if (titleOpts.length>0) { detail += '\n\n' + __('AI title options') + ':'; titleOpts.forEach(function(opt,i){ detail+='\n  '+(i+1)+'. ['+opt.style+'] '+opt.title; }); }
        alert(detail);
    } catch(e) { console.error(e); alert(__('View failed')); }
}

// ===== image gallery =====
var _galleryItemId = null;
async function openImageGallery(itemId) {
    _galleryItemId = itemId;
    showModal('image-gallery-modal');
    await loadGalleryImages(itemId);
}
async function loadGalleryImages(itemId) {
    var container = document.getElementById('image-gallery-container');
    var countEl = document.getElementById('image-count');
    if (!container) return;
    try {
        var res = await axios.get('/admin/ali-api/items/'+itemId+'/images');
        if (!res.data.success) { container.innerHTML='<div class="ac dg" style="padding:20px">' + __('Failed to load') + ': ' + res.data.error + '</div>'; return; }
        var images = res.data.data.images||[];
        if (countEl) countEl.textContent = __('Total ') + images.length + __(' images');
        if (images.length===0) { container.innerHTML='<div class="ac dim" style="padding:20px;font-size:13px"><div style="font-size:28px;opacity:.4">&#x1F4E5;</div><p style="margin-top:8px">' + __('No images, click above to upload') + '</p></div>'; return; }
        container.innerHTML = '<div class="igg">'+images.map(function(img,idx){
            return '<div class="ig-item"><img src="'+escHtml(img.url)+'" class="ig-img" alt="' + __('Image') + '" onerror="safeImgOnError.call(this,\'large\')"><div class="ig-ft"><span class="dim">'+(idx+1)+'</span><button class="btn bs" onclick="deleteGalleryImage('+itemId+','+idx+')" title="' + __('Delete') + '" style="color:#f85149">&times;</button></div></div>';
        }).join('')+'</div>';
    } catch(e) { container.innerHTML='<div class="ac dg" style="padding:20px">' + __('Failed to load') + ': ' + e.message + '</div>'; }
}
async function deleteGalleryImage(itemId, index) {
    if (!confirm(__('Delete image #') + (index+1) + __('?'))) return;
    try { showLoading(); var res=await axios.delete('/admin/ali-api/items/'+itemId+'/images/'+index); if (res.data.success){ await loadGalleryImages(itemId); if (currentPage==='products') loadProducts(currentProductPage); } else alert(__('Delete failed') + ': ' + res.data.error); } catch(e){ alert(__('Delete failed') + ': ' + (e.response?.data?.error||e.message)); } finally { hideLoading(); }
}
async function uploadGalleryImages(itemId, files) {
    if (!files||files.length===0) return;
    for (var i=0;i<files.length;i++) {
        var f=files[i];
        if (f.size>5*1024*1024) { alert(__('File ') + f.name + __(' exceeds 5MB limit')); continue; }
        var ext=f.name.split('.').pop().toLowerCase();
        if (!['png','jpg','jpeg','gif','webp'].includes(ext)) { alert(__('File ') + f.name + __(' format not supported')); continue; }
        var fd=new FormData(); fd.append('file',f);
        try { showLoading(); var r=await axios.post('/admin/ali-api/items/'+itemId+'/images/upload',fd,{headers:{'Content-Type':'multipart/form-data'}}); if(!r.data.success) alert(__('Upload ') + f.name + __(' failed') + ': ' + r.data.error); } catch(e){ alert(__('Upload ') + f.name + __(' failed') + ': ' + (e.response?.data?.error||e.message)); } finally { hideLoading(); }
    }
    await loadGalleryImages(itemId);
    if (currentPage==='products') loadProducts(currentProductPage);
}

// ===== collect =====
async function collectSingleProduct() {
    var pid = document.getElementById('product-id')?.value?.trim();
    if (!pid) { showMessage('collect-result', __('Please enter the product ID'), 'error'); return; }
    try {
        showLoading();
        var res = await axios.post('/admin/ali-api/items/collect', {product_id:pid});
        if (res.data.success) { var tag = res.data.data.from_cache ? __('(from cache)') : ''; showMessage('collect-result', __('Collected') + tag + ' ' + __('Product ID') + ': ' + res.data.data.item_id, 'success'); document.getElementById('product-id').value=''; if (currentPage==='dashboard') loadDashboard(); }
        else showMessage('collect-result', __('Collection failed') + ': ' + res.data.error, 'error');
    } catch(e) { showMessage('collect-result', __('Collection failed') + ': ' + (e.response?.data?.error||e.message), 'error'); }
    finally { hideLoading(); }
}
async function searchCollect() {
    var kw = document.getElementById('search-keywords')?.value?.trim();
    if (!kw) { showMessage('search-result', __('Enter search keywords'), 'error'); return; }
    var page = document.getElementById('search-page')?.value||1;
    var size = document.getElementById('search-size')?.value||20;
    try {
        showLoading();
        var res = await axios.post('/admin/ali-api/items/search', {keywords:kw,page_no:parseInt(page),page_size:parseInt(size)});
        if (res.data.success) {
            var d=res.data.data;
            var msg = __('Search success: found ') + d.total + __(' products');
            if (d.products?.length>0) { msg += '<br><br><b>' + __('Preview') + ':</b><br>'; d.products.slice(0,5).forEach(function(p,i){ msg+=(i+1)+'. '+escHtml(p.title)+' - '+formatPrice(p.price)+'<br>'; }); if (d.products.length>5) msg+='...' + __('and ') + (d.products.length-5) + __('items'); }
            showMessage('search-result',msg,'success');
        } else showMessage('search-result', __('Search failed') + ': ' + res.data.error, 'error');
    } catch(e) { showMessage('search-result', __('Search failed') + ': ' + (e.response?.data?.error||e.message), 'error'); }
    finally { hideLoading(); }
}

// ===== AI titles =====
async function generateAiTitles(itemId) {
    if (!confirm(__('Generate AI title options?'))) return;
    try {
        showLoading();
        var res = await axios.post('/admin/ali-api/items/'+itemId+'/ai-titles');
        if (res.data.success) { showTitleSelectionModal(itemId, res.data.data.ai_title_options); }
        else alert(__('AI title generation failed') + ': ' + res.data.error);
    } catch(e) { alert(__('AI title generation failed') + ': ' + (e.response?.data?.error||e.message)); }
    finally { hideLoading(); }
}
function showTitleSelectionModal(itemId, options) {
    currentAiItemId=itemId;
    var list=document.getElementById('ai-title-options');
    if (!list) return;
    if (!options||options.length===0) { list.innerHTML='<div class="dim" style="font-size:12px">' + __('No valid AI title options') + '</div>'; }
    else {
        list.innerHTML = options.map(function(opt,idx){
            return '<div class="to" onclick="selectAiTitle(this,'+opt.id+')" data-title="'+escHtml(opt.title)+'"><div class="to-badges"><span class="bdg gy">' + __('Option') + (idx+1) + '</span><span class="bdg b">'+styleLabel(opt.style)+'</span>'+(idx===0?'<span class="bdg y">' + __('Recommended') + '</span>':'')+'</div><div style="font-size:13px;margin-bottom:2px;font-weight:600">'+escHtml(opt.title)+'</div><div class="dim" style="font-size:11px">'+escHtml(opt.reason||'')+'</div></div>';
        }).join('');
    }
    document.getElementById('confirm-title-btn').onclick = async function() {
        var sel=document.querySelector('.to.sel');
        if (!sel) { alert(__('Please select a title')); return; }
        await confirmSelectedTitle(itemId, sel.getAttribute('data-title'));
    };
    showModal('ai-title-modal');
}
function styleLabel(s) {
    var m = { 'professional': __('Professional'), 'attractive': __('Attractive'), 'concise': __('Concise'), 'normal': __('Generic') };
    return m[s]||s;
}
function selectAiTitle(el) {
    document.querySelectorAll('.to').forEach(function(e){ e.classList.remove('sel'); });
    el.classList.add('sel');
}
async function confirmSelectedTitle(itemId, title) {
    try {
        showLoading();
        var res = await axios.post('/admin/ali-api/items/'+itemId+'/select-title', {title:title});
        if (res.data.success) { alert(__('Title selected!')); closeModal('ai-title-modal'); loadProducts(currentProductPage); }
        else alert(__('Selection failed') + ': ' + res.data.error);
    } catch(e) { alert(__('Selection failed') + ': ' + (e.response?.data?.error||e.message)); }
    finally { hideLoading(); }
}

// ===== publish =====
async function publishProduct(itemId) {
    var stock = prompt(__('Enter stock quantity (default 999)'), '999'); if (stock===null) return;
    var n=parseInt(stock)||999;
    if (!confirm(__('Publish to local store? Stock: ') + n)) return;
    try {
        showLoading();
        var res=await axios.post('/admin/ali-api/items/'+itemId+'/publish',{stock:n});
        if (res.data.success) { alert(__('Published! Local ID: ') + res.data.data.target_product_id + '\n' + __('Title') + ': ' + res.data.data.title + '\n' + __('Price') + ': ' + formatPrice(res.data.data.price)); loadProducts(currentProductPage); }
        else alert(__('Publish failed') + ': ' + res.data.error);
    } catch(e) { alert(__('Publish failed') + ': ' + (e.response?.data?.error||e.message)); }
    finally { hideLoading(); }
}

// ===== logs =====
async function loadLogs(page) {
    if (page===undefined) page=currentLogPage;
    try {
        showLoading();
        var ep=document.getElementById('log-endpoint')?.value||'';
        var sc=document.getElementById('log-success')?.value||'';
        var url='/admin/ali-api/logs?page='+page+'&per_page=20';
        if (ep) url+='&endpoint='+encodeURIComponent(ep);
        if (sc) url+='&success='+sc;
        var res=await axios.get(url);
        if (res.data.success) { updateLogsTable(res.data.data.logs); updateLogsPagination(res.data.data.pagination); currentLogPage=page; }
    } catch(e) { console.error('load logs failed',e); }
    finally { hideLoading(); }
}
function updateLogsTable(logs) {
    var tbody=document.getElementById('logs-table'); if (!tbody) return;
    if (!logs||logs.length===0) { tbody.innerHTML='<tr><td colspan="8" class="tc dim" style="padding:20px">' + __('No logs') + '</td></tr>'; return; }
    tbody.innerHTML=logs.map(function(log){
        return '<tr><td>'+log.id+'</td><td>'+(log.user_id||__('System'))+'</td><td><code style="color:var(--accent);font-size:11px">'+escHtml(log.endpoint)+'</code></td><td><span class="dim" style="font-size:10px">'+escHtml(JSON.stringify(parseJsonField(log.params,{})).substring(0,40))+'</span></td><td>'+(log.response_code||'-')+'</td><td>'+(log.response_time||'-')+'</td><td>'+(log.success?'<span class="stbd on">' + __('Success') + '</span>':'<span class="stbd off">' + __('Failed') + '</span>')+'</td><td>'+formatDate(log.created_at)+'</td></tr>';
    }).join('');
}
function updateLogsPagination(p) {
    var el=document.getElementById('logs-pagination'); if (!el) return;
    var page=p.page,total=p.total_pages;
    var h='';
    h+=page>1?'<a onclick="loadLogs('+(page-1)+');return false;">' + __('Previous') + '</a>':'<a class="dis">' + __('Previous') + '</a>';
    var start=Math.max(1,page-2),end=Math.min(total,page+2);
    for(var i=start;i<=end;i++) h+=i===page?'<a class="act">'+i+'</a>':'<a onclick="loadLogs('+i+');return false;">'+i+'</a>';
    h+=page<total?'<a onclick="loadLogs('+(page+1)+');return false;">' + __('Next') + '</a>':'<a class="dis">' + __('Next') + '</a>';
    el.innerHTML=h;
}

// ===== cache =====
async function loadCacheStats() {
    try { var res=await axios.get('/admin/ali-api/cache/stats'); if(res.data.success) updateCacheDetails(res.data.data); } catch(e){ console.error(e); }
}
function updateCacheDetails(s) {
    var el=document.getElementById('cache-details'); if(!el) return;
    var rc=s.redis?.connected;
    el.innerHTML='<div style="font-size:12px;margin-bottom:6px"><b>Redis</b><br>' + __('Status') + ': ' + (rc?'<span class="stbd on">' + __('Connected') + '</span>':'<span class="stbd off">' + __('Not connected') + '</span>') + (rc?'<br>' + __('Memory') + ': ' + (s.redis.used_memory||'N/A') + '<br>' + __('Connections') + ': ' + (s.redis.connected_clients||0):'<br><span class="dim">' + __('Using memory cache') + '</span>') + '</div><div style="font-size:12px"><b>' + __('Memory cache') + '</b><br>' + __('Entries') + ': ' + (s.memory?.size??0) + '/' + (s.memory?.maxsize??'-') + '<br>' + __('Expired') + ': ' + (s.memory?.expired_entries??0) + '<br>TTL: ' + (s.memory?.ttl??'-') + __('s') + '</div><div style="font-size:12px;margin-top:6px"><b>' + __('Use Redis') + ':</b> ' + (s.use_redis?__('Yes'):__('No')) + '</div>';
}

// ===== config =====
async function loadConfig() {
    try {
        var res=await axios.get('/admin/ali-api/config');
        if (!res.data.success) return;
        var c=res.data.data;
        var gw=document.getElementById('cfg-api-gateway'); if(gw) gw.value=c.alibaba.api_gateway||'';
        var ver=document.getElementById('cfg-api-version'); if(ver) ver.value=c.alibaba.api_version||'';
        var sig=document.getElementById('cfg-sign-method'); if(sig) sig.value=c.alibaba.sign_method||'';
        var key=document.getElementById('cfg-app-key'); if(key) key.value=c.alibaba.app_key_masked||'';
        var sec=document.getElementById('cfg-app-secret'); if(sec) sec.placeholder = c.alibaba.app_key_configured ? __('Configured, enter new value to overwrite') : __('Enter 1688 AppSecret');
        var rl=document.getElementById('rate-limit-config');
        if (rl&&c.rate_limit) rl.innerHTML = '<dt>' + __('User daily limit') + '</dt><dd>'+c.rate_limit.user_daily_limit+'</dd><dt>' + __('User hourly limit') + '</dt><dd>'+c.rate_limit.user_hourly_limit+'</dd><dt>' + __('Global concurrency') + '</dt><dd>'+c.rate_limit.global_concurrent_limit+'</dd><dt>' + __('Global QPS') + '</dt><dd>'+c.rate_limit.global_qps_limit+'</dd><dt>' + __('Circuit breaker threshold') + '</dt><dd>'+c.rate_limit.circuit_breaker_threshold+'</dd>';
        var ca=document.getElementById('cache-config');
        if (ca&&c.cache) ca.innerHTML = '<dt>Redis</dt><dd><span class="stbd '+(c.cache.redis_configured?'on':'off')+'">'+(c.cache.redis_configured?__('Configured'):__('Not configured'))+'</span></dd><dt>' + __('Memory cache') + '</dt><dd>'+c.cache.memory_cache_maxsize+'</dd><dt>' + __('Product cache TTL') + '</dt><dd>'+c.cache.product_cache_ttl + __('s') + '</dd>';
    } catch(e) { console.error('load config failed',e); }
}

// ===== save config =====
async function saveConfig() {
    var ak=document.getElementById('cfg-app-key')?.value?.trim();
    var as=document.getElementById('cfg-app-secret')?.value?.trim();
    var ag=document.getElementById('cfg-api-gateway')?.value?.trim();
    if (!ak&&!as&&!ag) { showMessage('config-save-result', __('Fill in AppKey, AppSecret or API Gateway URL'), 'error'); return; }
    if (ak&&ak.endsWith('...')) { showMessage('config-save-result', __('AppKey is masked; enter full key to update'), 'error'); return; }
    var btn=document.getElementById('save-config-btn');
    if (btn) { btn.disabled=true; btn.innerHTML = __('Saving...'); }
    try {
        var res=await axios.post('/admin/ali-api/config',{app_key:ak,app_secret:as,api_gateway:ag});
        if (res.data.success) { showMessage('config-save-result',res.data.message,'success'); loadConfig(); }
        else showMessage('config-save-result', __('Save failed') + ': ' + (res.data.error||''), 'error');
    } catch(e) { showMessage('config-save-result', __('Save failed') + ': ' + (e.response?.data?.error||e.message), 'error'); }
    finally { if(btn){btn.disabled=false;btn.innerHTML = __('Save configuration');} }
}

// ===== helpers =====
function escHtml(s) { if (!s) return ''; var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function safeImgOnError(size) {
    var svg=size==='large'?'<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180"><rect fill="#111" width="180" height="180"/><text x="55" y="95" font-size="14" fill="#555">' + __('Failed to load') + '</text></svg>':'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect fill="#111" width="80" height="80"/><text x="25" y="45" font-size="12" fill="#555">' + __('No image') + '</text></svg>';
    this.src='data:image/svg+xml,'+encodeURIComponent(svg);
}
function parseJsonField(v,d) {
    if (v===null||v===undefined) return d;
    if (typeof v==='string') { try { return JSON.parse(v); } catch(e) { return d; } }
    return v;
}

// ===== 代发订单 =====
var poPage=1;

async function loadPurchaseOrders() {
    try {
        var status = document.getElementById('po-status-filter').value;
        var res = await api('/admin/ali-api/orders/supplier?status='+status+'&page='+poPage+'&limit=20');
        if (!res.success) return;
        var data = res.data;
        var tbody = document.getElementById('po-tbody');
        if (!data.items || data.items.length===0) {
            tbody.innerHTML='<tr><td colspan="8" class="tc dim" style="padding:20px">' + __('No drop-shipping orders') + '</td></tr>';
            document.getElementById('po-pagination').innerHTML='';
            return;
        }
        var h='';
        data.items.forEach(function(po){
            var statusBadge = getStatusBadge(po.ali_order_status);
            var buyLink = '';
            var trackBtn = '';
            if (po.ali_order_status==='pending') {
                buyLink = '<button class="btn bn btn-sm" onclick="createPurchaseOrder('+po.id+')">' + __('One-click Purchase') + '</button>';
            } else {
                buyLink = '<span class="dim" style="font-size:11px">'+po.ali_order_id+'</span>';
            }
            if (po.ali_order_status==='ordered') {
                trackBtn = '<button class="btn bs btn-sm mt" style="margin-left:4px" onclick="syncTracking('+po.id+')">' + __('Sync Tracking') + '</button>';
            }
            var tracking = po.tracking_company ? (po.tracking_company+' '+po.tracking_number) : '-';
            if (po.ali_order_status==='shipped' && tracking!=='-') {
                tracking = '<span style="color:var(--grn)">'+tracking+'</span>';
            }
            h+='<tr>' +
                '<td style="font-size:11px">#'+po.id+'</td>' +
                '<td><a href="/admin/orders/'+po.local_order_item_id+'/detail" target="_blank" style="font-size:11px">'+esc(po.local_order_id||'')+'</a><br><span class="dim" style="font-size:10px">'+getOrderStatusText(po.order_status||po.local_status)+'</span></td>' +
                '<td>'+(po.prod_thumb ? '<img src="'+esc(po.prod_thumb)+'" style="width:40px;height:40px;border-radius:4px;object-fit:cover;vertical-align:middle;margin-right:6px">' : '')+'<span style="font-size:12px">'+esc(po.prod_title||'')+'</span><br><span class="dim" style="font-size:10px">x'+po.quantity+'</span></td>' +
                '<td style="font-size:11px">'+esc(po.buyer_phone||'')+'<br><span class="dim">'+esc(po.buyer_username||'')+'</span></td>' +
                '<td><a href="https://detail.1688.com/offer/'+po.ali_product_id+'.html" target="_blank" style="font-size:11px">'+po.ali_product_id+'</a></td>' +
                '<td>'+statusBadge+'</td>' +
                '<td style="font-size:11px">'+tracking+'</td>' +
                '<td style="white-space:nowrap">'+buyLink+trackBtn+'</td>' +
                '</tr>';
        });
        tbody.innerHTML=h;
        // 分页
        var totalPages = Math.ceil(data.total/20);
        var ph='';
        if (poPage>1) ph+='<a onclick="poPage='+(poPage-1)+';loadPurchaseOrders();return false;">'+__('Previous')+'</a>';
        for (var i=Math.max(1,poPage-2); i<=Math.min(totalPages,poPage+2); i++) {
            ph+='<a'+(i===poPage?' class="act"':'')+' onclick="poPage='+i+';loadPurchaseOrders();return false;">'+i+'</a>';
        }
        if (poPage<totalPages) ph+='<a onclick="poPage='+(poPage+1)+';loadPurchaseOrders();return false;">'+__('Next')+'</a>';
        document.getElementById('po-pagination').innerHTML=ph;
    } catch(e) {
        console.error('loadPurchaseOrders error:', e);
    }
}

function getOrderStatusText(s) {
    var m={'pending':__('Pending payment'),'paid':__('Paid'),'shipped':__('Shipped'),'completed':__('Completed'),'cancelled':__('Cancelled'),'refunded':__('Refunded')};
    return m[s]||s||'';
}

function getStatusBadge(status) {
    var colors={'pending':'var(--wrn)','ordered':'var(--blu)','shipped':'var(--grn)','received':'var(--grn)','cancelled':'var(--dim)'};
    var labels={'pending':__('Pending procurement'),'ordered':__('Ordered'),'shipped':__('Shipped'),'received':__('Completed'),'cancelled':__('Cancelled')};
    return '<span style="color:'+(colors[status]||'var(--dim)')+';font-size:11px;font-weight:600">'+(labels[status]||status)+'</span>';
}

async function createPurchaseOrder(poId) {
    if (!confirm(__('Create 1688 purchase order? Verify price and stock.'))) return;
    try {
        var res = await api('/admin/ali-api/orders/create-purchase', 'POST', {purchase_order_id: poId});
        if (res.success) {
            alert(__('Purchase order submitted to 1688, order# ') + res.data.ali_order_id);
            loadPurchaseOrders();
        } else {
            alert(__('Order failed') + ': ' + (res.error||__('Unknown error')));
        }
    } catch(e) {
        alert(__('Order failed') + ': ' + e.message);
    }
}

async function syncTracking(poId) {
    try {
        var res = await api('/admin/ali-api/orders/sync-tracking', 'POST', {purchase_order_id: poId});
        if (res.success) {
            if (res.data.status==='shipped') {
                alert(__('Tracking synced: ') + res.data.tracking_company + ' ' + res.data.tracking_number);
            } else {
                alert(__('1688 has not shipped yet'));
            }
            loadPurchaseOrders();
        } else {
            alert(__('Sync failed') + ': ' + (res.error||__('Unknown error')));
        }
    } catch(e) {
        alert(__('Sync failed') + ': ' + e.message);
    }
}

// 过滤器变更自动刷新
document.addEventListener('DOMContentLoaded', function(){
    var filter = document.getElementById('po-status-filter');
    if (filter) {
        filter.addEventListener('change', function(){
            poPage=1;
            if (document.getElementById('supplier-page').style.display!=='none') {
                loadPurchaseOrders();
            }
        });
    }
    var refreshBtn = document.getElementById('refresh-po-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', function(){
            loadPurchaseOrders();
        });
    }
});
