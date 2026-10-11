<?php
// The database, access token, exports and Python package live outside the webroot.
header('Content-Type: application/json');
header('Cache-Control: no-store');
function fail_request($code, $message) {
    http_response_code($code);
    echo json_encode(['error' => $message]);
    exit;
}
if ($_SERVER['REQUEST_METHOD'] !== 'GET') fail_request(405, 'method_not_allowed');
$private = dirname(__DIR__, 2) . '/private_html/growth-engine';
$config = json_decode(@file_get_contents($private . '/config.json'), true);
if (!is_array($config) || empty($config['token'])) fail_request(503, 'runtime_unavailable');
$authorization = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
if (!hash_equals('Bearer ' . $config['token'], $authorization)) fail_request(401, 'unauthorized');
$route = $_GET['route'] ?? 'health';
if (!in_array($route, ['health', 'historical'], true)) fail_request(404, 'not_found');
if (!function_exists('curl_init')) fail_request(503, 'runtime_unavailable');
$path = $route === 'health' ? '/healthz' : '/v1/historical-evaluation';
$handle = curl_init('http://127.0.0.1:8765' . $path);
curl_setopt_array($handle, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 30,
    CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $config['token']]]);
$output = curl_exec($handle);
$status = curl_getinfo($handle, CURLINFO_HTTP_CODE);
curl_close($handle);
if ($status !== 200 || !is_array(json_decode($output ?: '', true))) fail_request(503, 'evaluation_unavailable');
echo $output;
