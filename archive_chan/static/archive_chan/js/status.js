google.load("visualization","1",{packages:["corechart"]});google.setOnLoadCallback(update);var statsData,timeout;$(window).resize(function(){drawChart(statsData)});$(document).ready(function(){$(".last-update-info").on("click","#update-now",function(){update()})});function update(){clearTimeout(timeout);$("#update-now i").addClass("fa-spin");var a=$.ajax({url:info_data.api_url,dataType:"json"}).done(function(c){statsData=c.chart_data;drawData(c);drawChart(c.chart_data);var b=new Date();$("#last-update").attr("datetime",b.toISOString()).attr("title",b).data("timeago",null).timeago()}).fail(function(b,d,c){console.log(b.responseText);console.log(d);console.log(c)}).always(function(){timeout=setTimeout(update,30*1000);$("#update-now i").removeClass("fa-spin")})}function drawData(a){$("#value-total-threads").text(a.total_threads)}function drawChart(d){var a={fontSize:12,chartArea:{left:50,top:10,right:100,bottom:0,width:"100%"},legend:{position:"none"},tooltip:{isHtml:true},series:[{color:"#65c6bb"}],curveType:"function"};var c=new google.visualization.DataTable(d);var b=new google.visualization.LineChart(document.getElementById("chart"));b.draw(c,a)};