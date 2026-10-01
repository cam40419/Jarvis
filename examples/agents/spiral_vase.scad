/* LILT — a twisted, fluted vase. All dimensions are millimeters.
   Editable source: adjust the parameters below, then render/export in OpenSCAD.
   The inside is offset along the outer surface normal, not merely radially.
   Both shell surfaces join across the rim and through a solid 3 mm floor.
   Decorative/FDM object: use a removable liner for water until tested/sealed.
*/
height = 180;
wall = 2.4;
base = 3;
flutes = 18;
flute_depth = 1.35;
twist = 100;
sides = 288;
outer_steps = 120;
inner_steps = 118;

function clamp(v,a,b) = min(b,max(a,v));
function smooth(v) = let(t=clamp(v,0,1)) t*t*(3-2*t);
function amp(z) = flute_depth * smooth(z/12) * (0.38+0.62*smooth((height-z)/8));
function radius(z,a) = 34 + 12*pow(sin(180*z/height),2) + 4*z/height
                       + amp(z)*cos(flutes*(a-twist*z/height));
function point(z,a) = [radius(z,a)*cos(a),radius(z,a)*sin(a),z];
function dz(z,a) = (radius(z+0.01,a)-radius(z-0.01,a))/0.02;
// Convert the angle derivative from degrees to radians.
function da(z,a) = (radius(z,a+0.01)-radius(z,a-0.01))/0.02*180/PI;
function normal(z,a) = let(r=radius(z,a), t=da(z,a)/r, v=dz(z,a))
    [cos(a)+t*sin(a),sin(a)-t*cos(a),-v]/sqrt(1+t*t+v*v);
function inner(z,a) = let(p=point(z,a)-wall*normal(z,a))
    [p.x,p.y,z==base ? base : z==height ? height : p.z];
function oi(i,j) = i*sides+(j%sides);
inner_start = (outer_steps+1)*sides;
function ii(i,j) = inner_start+i*sides+(j%sides);
bottom_center = inner_start+(inner_steps+1)*sides;
inside_center = bottom_center+1;

points = concat(
    [for(i=[0:outer_steps],j=[0:sides-1]) point(height*i/outer_steps,360*j/sides)],
    [for(i=[0:inner_steps],j=[0:sides-1]) inner(base+(height-base)*i/inner_steps,360*j/sides)],
    [[0,0,0],[0,0,base]]
);
// OpenSCAD polyhedron face winding uses the left-hand convention.
faces = concat(
    [for(i=[0:outer_steps-1],j=[0:sides-1]) each
        [[oi(i,j),oi(i+1,j+1),oi(i,j+1)],[oi(i,j),oi(i+1,j),oi(i+1,j+1)]]],
    [for(i=[0:inner_steps-1],j=[0:sides-1]) each
        [[ii(i,j),ii(i,j+1),ii(i+1,j+1)],[ii(i,j),ii(i+1,j+1),ii(i+1,j)]]],
    [for(j=[0:sides-1]) each
        [[oi(outer_steps,j),ii(inner_steps,j),ii(inner_steps,j+1)],
         [oi(outer_steps,j),ii(inner_steps,j+1),oi(outer_steps,j+1)]]],
    [for(j=[0:sides-1]) [bottom_center,oi(0,j),oi(0,j+1)]],
    [for(j=[0:sides-1]) [inside_center,ii(0,j+1),ii(0,j)]]
);
polyhedron(points=points,faces=faces,convexity=12);
